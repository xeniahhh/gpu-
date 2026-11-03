# S5 路径与带宽指标

## S5 在做什么

S5 把 S3/S4 的 GPU 放置结果转换为抽象 fabric 流量，并计算：

- 跨节点通信路径。
- 每条链路的请求量、分配量和利用率。
- 每个作业的吞吐代理。
- leaf 超订比。
- balanced leaf 二分带宽代理。
- 作业和全局瓶颈链路。

S5 不是报文级网络仿真。所有流量采用等权单位，同一链路上的流量按比例公平共享。
实际公式和参数会明确输出，方便手工核对。

## 流量模型

### ring_all_reduce

放置后的 GPU 按分配结果形成环。只有相邻 rank 位于不同节点时，才产生 fabric
流量。同一无序节点对之间的多个环边合并成一个流量组。

### all_to_all

按节点汇总参与 GPU。对于两个具有 `a` 和 `b` 个 GPU 的节点，产生：

```text
a * b traffic units
```

同节点内部的 GPU 对不消耗 fabric 链路。

## 路径选择

路径使用无向链路图中的最短跳数路径：

```text
同 leaf:     node -> leaf -> node
跨 leaf:     node -> leaf -> spine -> leaf -> node
```

存在多条等价 spine 路径时，依次比较：

1. 加入该流量后最大的链路利用率。
2. 路径上链路利用率之和。
3. 路径链路 ID 序列，保证确定性。

## 带宽公式

每个 traffic unit 的默认请求带宽：

```text
flow_unit_gbps = 10 Gbps
```

链路请求量：

```text
requested = traffic_units * flow_unit_gbps
```

链路分配量：

```text
allocated = min(requested, capacity)
```

利用率：

```text
utilization = requested / capacity
```

流量组按路径上的瓶颈链路公平共享。作业的 `throughput_proxy_gbps` 是该作业所有
流量组中，每个 traffic unit 可获得的瓶颈带宽的最小值。只在单节点内通信时，该
字段为 `null`。

## Leaf 指标

超订比：

```text
ratio = leaf-node 下联总容量 / leaf-spine 上联总容量
```

默认生成拓扑为：

```text
下联 8 node * 3200 Gbps = 25600 Gbps
上联 8 spine * 400 Gbps = 3200 Gbps
ratio = 8:1
```

二分带宽代理将 leaf 按 ID 排序并均分为两半，取两侧上联容量较小的一侧。它不是
完整的图 min-cut 求解，而是 MVP 使用的可复现跨 spine 容量代理。

## 如何查看结果

默认使用 rail-aware 放置：

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s5.py
```

切换 first-fit：

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s5.py --strategy first_fit
```

提高每个流量单位的请求量以观察拥塞：

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s5.py --flow-unit-gbps 200
```

完整 JSON：

```powershell
.\.venv\Scripts\python.exe scripts\inspect_s5.py --json
```

## 独立验收

```powershell
.\.venv\Scripts\python.exe scripts\run_tests.py
```

S5 测试覆盖 ring/all-to-all 流量、跨 leaf 路径、无拥塞吞吐、拥塞瓶颈、leaf
超订比、二分带宽代理、单节点作业、确定性和结果查看命令。
