from copy import deepcopy
import unittest

from fabric_sim import simulate
from fabric_sim.ddp import demo_ddp
from fabric_sim.tp import demo_tp
from fabric_sim.validation import canonical_hash, validate_spec


class ContractV2Tests(unittest.TestCase):
    def test_unhashable_values_are_controlled_validation_errors(self):
        mutations = [lambda s: s.update(scenario=[]),
                     lambda s: s['jobs'][0].update(gpu_ids=[{},'n0:gpu1','n1:gpu0','n1:gpu1']),
                     lambda s: s.update(faults=[{'at_ms':1,'type':'node_down','target_id':{}}]),
                     lambda s: s['profile'].update(source=[]),
                     lambda s: s.update(placement_policy={})]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                spec=demo_ddp();mutation(spec)
                with self.assertRaises(ValueError):validate_spec(spec)

    def test_normalized_tp_defaults_execute(self):
        spec=demo_tp('tp1');spec['model']={}
        del spec['replicas'][0]['tp_size']
        for r in spec['requests']:
            del r['prompt_tokens'];del r['max_new_tokens']
        frozen=validate_spec(spec)
        self.assertEqual(frozen['model']['hidden_size'],3584)
        result=simulate(frozen)
        self.assertTrue(all(x['state']=='completed' for x in result['requests']))

    def test_hash_binds_configuration_and_input_is_copied(self):
        spec=demo_ddp();original=deepcopy(spec)
        frozen=validate_spec(spec);digest=canonical_hash(frozen)
        spec['topology']['uplink_bandwidth_gbps']=25
        self.assertEqual(frozen,original)
        self.assertNotEqual(canonical_hash(spec),digest)

    def test_dtype_and_false_measurement_fail_preflight(self):
        spec=demo_ddp();spec['profile']['dtype']='float16'
        with self.assertRaises(ValueError):validate_spec(spec)
        spec=demo_tp();spec['profile']={'source':'MEASURED'}
        with self.assertRaises(ValueError):validate_spec(spec)

    def test_excessive_work_is_bounded(self):
        spec=demo_tp('tp4')
        spec['requests']=[{'id':f'q{i}','arrival_ms':0,'prompt_tokens':64,'max_new_tokens':256} for i in range(64)]
        with self.assertRaises(ValueError):validate_spec(spec)

    def test_input_and_implementation_fingerprints_are_present(self):
        spec=demo_ddp('compact');spec['jobs']=spec['jobs'][:1];spec['jobs'][0]['steps']=1
        result=simulate(spec)
        self.assertEqual(result['input_hash'],canonical_hash(validate_spec(spec)))
        self.assertEqual(len(result['implementation_hash']),64)
        self.assertEqual(result['measurement_status'],'SIMULATION_NOT_GPU_MEASUREMENT')


if __name__=='__main__':unittest.main()
