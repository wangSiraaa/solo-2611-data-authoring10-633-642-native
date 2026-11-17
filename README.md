# 序列化返修与出库输入

case.json是完整微型样例。items的assembly只有id/kind/type，part只有id/kind/lot；edges中每对为(child,parent)，不是(parent,child)。change中的三元组为(动作,child,parent)，按数组顺序原子提交。A/B为两台同种meter根总成，good/bad为不同物理零件，批次L-good/L-bad与客户C1/订单O1完全明确关联。recall_lots是在拟出库之前已宣布的召回批次。before与change后是同一ship-1拟出库请求的两种输入状态，对照不表示实际连续出库两次。该case只含起点/操作数据，没有目标生产实现；题面要求其他合法场景自行补充。
