case.json给一类别权重1的评分微型输入，满分由rules固定，attempts为不同尝试；候选仍需实现题面镜像修订/撤回等全族。座位坐标不压缩，a1为唯一无障碍座位，A/B不允许相邻。输入不提供评分器或排座目标代码。

本微型输入的完整映射：全学生为A/B，只有一个评分类别C，权重[1,1]，assessments均属于C，id就是assessment_id。attempts的id就是attempt_id，student/assessment引用既有身份，各是首次完整镜像revision=1、void=false，事件号e-加id，顺序为数组顺序。added_attempt为新的独立尝试同样初版；每一座位均available=true，registration为exam_id=E的有效报名，未列的特殊需求均false。
