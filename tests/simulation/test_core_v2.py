"""Analytical and adversarial checks of the shared physical-resource model."""
from __future__ import annotations

import unittest

from fabric_sim.core import Engine, Network, ring_allreduce


def topology(count=4, gpus=1, rate=100, latency=0):
    return {"nodes": [{"id": f"n{i}", "rack_id": "r0", "gpu_count": gpus, "memory_gib": 24} for i in range(count)],
            "node_bandwidth_gbps": rate, "uplink_bandwidth_gbps": rate,
            "local_bandwidth_gbps": rate, "link_latency_us": latency, "local_latency_us": latency}


class CoreV2Tests(unittest.TestCase):
    def test_ring_matches_byte_conservation_and_analytical_time(self):
        net = Network(topology())
        engine = Engine(net)
        done = []
        gradient = 64 * 1024**2
        ring_allreduce(engine, "job", list(net.gpus), gradient, lambda: done.append(engine.now_ms))
        engine.run(1000)
        self.assertEqual(len(done), 1)
        self.assertAlmostEqual(done[0], 2 * 3 * (gradient / 4) / (100e9 / 8) * 1000, places=9)
        self.assertAlmostEqual(engine.stats()["total_sent_bytes"], 2 * 3 * gradient, places=5)
        self.assertEqual(engine.stats()["completed_flows"], 24)
        rounds = [e for e in engine.events if e['kind'] == 'collective_round_started']
        self.assertEqual([e['phase'] for e in rounds], ['reduce_scatter'] * 3 + ['all_gather'] * 3)
        self.assertEqual(engine.stats()['max_capacity_ratio'], 1.0)

    def test_rates_reallocate_when_short_competing_flow_finishes(self):
        engine = Engine(Network(topology(count=3, rate=8)))
        times = {}
        engine.transfer('a', 'n0:gpu0', 'n1:gpu0', 1_000_000, lambda: times.update(a=engine.now_ms))
        engine.transfer('b', 'n0:gpu0', 'n2:gpu0', 3_000_000, lambda: times.update(b=engine.now_ms))
        engine.run(100)
        self.assertAlmostEqual(times['a'], 2)
        self.assertAlmostEqual(times['b'], 4)
        self.assertTrue(all(x['allocated_gbps'] <= x['capacity_gbps'] + 1e-8 for x in engine.resource_samples))

    def test_full_duplex_directions_have_independent_capacity(self):
        engine = Engine(Network(topology(count=2, rate=8)))
        times = []
        engine.transfer('a', 'n0:gpu0', 'n1:gpu0', 1_000_000, lambda: times.append(engine.now_ms))
        engine.transfer('b', 'n1:gpu0', 'n0:gpu0', 1_000_000, lambda: times.append(engine.now_ms))
        engine.run(100)
        self.assertEqual(times, [1.0, 1.0])

    def test_local_bus_is_finite_and_shared_both_directions(self):
        engine = Engine(Network(topology(count=1, gpus=2, rate=8)))
        done = []
        ring_allreduce(engine, 'job', ['n0:gpu0', 'n0:gpu1'], 1_000_000, lambda: done.append(engine.now_ms))
        engine.run(100)
        self.assertEqual(done, [2.0])
        self.assertEqual(engine.network.path('n0:gpu0', 'n0:gpu1'), ('node:n0:local',))

    def test_cancel_covers_pending_latency_and_old_collective(self):
        engine = Engine(Network(topology(count=2, latency=1000)))
        called = []
        ring_allreduce(engine, 'job', list(engine.network.gpus), 1_000_000, lambda: called.append('old'))
        engine.schedule(.5, lambda: engine.cancel_owner('job'), priority=-10)
        engine.run(100)
        self.assertEqual(called, [])
        self.assertEqual(engine.stats()['total_sent_bytes'], 0)
        self.assertEqual(engine.stats()['cancelled_flows'], 2)
        ring_allreduce(engine, 'job', list(engine.network.gpus), 1_000_000, lambda: called.append('new'))
        engine.run(100)
        self.assertEqual(called, ['new'])

    def test_failure_at_completion_time_prevents_completion_callback(self):
        engine = Engine(Network(topology(count=2, rate=8)))
        called = []
        engine.transfer('job', 'n0:gpu0', 'n1:gpu0', 1_000_000, lambda: called.append(True))
        engine.schedule(1, lambda: engine.cancel_owner('job'), priority=-10)
        engine.run(100)
        self.assertEqual(called, [])
        self.assertEqual(engine.stats()['cancelled_flows'], 1)

    def test_failed_resource_stalls_then_repair_resumes(self):
        engine = Engine(Network(topology(count=2, rate=8)))
        done = []
        engine.network.fail_resource('node:n0:tx')
        engine.transfer('job', 'n0:gpu0', 'n1:gpu0', 1_000_000, lambda: done.append(engine.now_ms))
        engine.schedule(5, lambda: engine.network.repair_resource('node:n0:tx'))
        engine.run(100)
        self.assertEqual(done, [6.0])

    def test_zero_rate_without_repair_ends_at_horizon(self):
        engine = Engine(Network(topology(count=2)))
        engine.network.fail_node('n0')
        engine.transfer('job', 'n0:gpu0', 'n1:gpu0', 100, lambda: self.fail('must not complete'))
        engine.run(10)
        self.assertEqual(engine.now_ms, 10)
        self.assertTrue(engine.stats()['horizon_reached'])
        self.assertEqual(engine.stats()['total_sent_bytes'], 0)

    def test_node_and_resource_failures_restore_independently(self):
        net = Network(topology(count=2))
        net.fail_node('n0')
        net.fail_resource('node:n0:tx')
        net.repair_node('n0')
        self.assertEqual(net.capacities['node:n0:tx'], 0)
        self.assertGreater(net.capacities['node:n0:rx'], 0)
        net.repair_resource('node:n0:tx')
        self.assertGreater(net.capacities['node:n0:tx'], 0)

    def test_cross_rack_uses_both_directed_uplinks(self):
        config = topology(count=2)
        config['nodes'][1]['rack_id'] = 'r1'
        net = Network(config)
        self.assertEqual(net.path('n0:gpu0', 'n1:gpu0'),
                         ('node:n0:tx','rack:r0:tx','rack:r1:rx','node:n1:rx'))

    def test_invalid_payloads_and_nonfinite_time_rejected(self):
        engine = Engine(Network(topology()))
        for value in [3, -1, float('inf'), True]:
            with self.assertRaises(ValueError):
                ring_allreduce(engine, 'job', list(engine.network.gpus), value, lambda: None)
        with self.assertRaises(ValueError):
            engine.schedule(float('nan'), lambda: None)

    def test_zero_time_callback_loop_is_bounded(self):
        engine = Engine(Network(topology()))
        def again(): engine.after(0, again)
        engine.after(0, again)
        with self.assertRaisesRegex(ValueError, 'event limit'):
            engine.run(100, max_events=10)

    def test_horizon_preserves_remaining_bytes_for_resume(self):
        engine = Engine(Network(topology(count=2, rate=8)))
        done = []
        engine.transfer('job', 'n0:gpu0', 'n1:gpu0', 2_000_000, lambda: done.append(engine.now_ms))
        engine.run(.5)
        self.assertEqual(engine.stats()['total_sent_bytes'], 500_000)
        self.assertEqual(done, [])
        engine.run(5)
        self.assertEqual(done, [2.0])
        self.assertEqual(engine.stats()['total_sent_bytes'], 2_000_000)

    def test_event_beyond_horizon_is_not_run_early(self):
        engine = Engine(Network(topology()))
        done = []
        engine.schedule(1e-10, lambda: done.append(True))
        engine.run(0)
        self.assertEqual(done, [])
        self.assertEqual(engine.now_ms, 0)


if __name__ == '__main__':
    unittest.main()
