# 分流和事实仓输入

case.json中L是层，E是实验，u1/u2是不同实验单位；config的buckets为左闭右开区间，variants数组保持给定顺序。所有at/visible/C/L/R/W都是整数秒；config1在0秒受理，u1于1秒访问，u2于2秒访问；config2另用于3秒改权重后的粘性检查。assignment别名L:u1表示该设施对(u1,L)首次真实提交的稳定分配身份，不暗示候选必须使用此格式。两条日志均来自u1；u2是真实分配但未曝光，必须进入分配人数。analysis_request中L是事件窗口起点0（不同于值为字符串L的layer字段）。e1的2秒曝光到c1的5秒消费恰好差W=3秒。id分别对应各来源event_id简写。样例不含目标生产代码；候选须完成题面全部合法输入合同。
