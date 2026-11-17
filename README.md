# 企业证据与借据输入

case.json的公司A/B/C/D是不同稳定原始法律主体。limit为同一虚构单位；principal/amount也用该单位整数。notes为已经存在的借据：A欠B80，C欠A50，未清偿；它们不重新经过新借据准入。merge_proof把B/C归为同集团，不改变原始借据端点。new_note_request是同一个拟提交请求n3（A欠B30），before/after对照不是连续提交两次。negative_probe和conflicting_proof用于单独测试证据冲突，主场景未预先加入它们。数组字段id对应题面的note_id/proof_id简写，left/right为公司端点，kind文字与题面相同。题面中的批次删除、清偿与历史查询仍需实现，不由case包办。
