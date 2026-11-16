"""Original synthetic contrasts, not a curriculum or scoring qualification set."""
import json
import socket
from types import SimpleNamespace

import pytest

from home_study_agent import document_independent_check, document_target_hypothesis
from home_study_agent.retrieval.local_semantic import LocalSemanticMatcher
from test_local_semantic import installed_models


BANK = {
    'math': [
        ('perimeter','长方形周长','长方形长8厘米、宽3厘米，求四条边长度的和。','给长8厘米宽3厘米的卡片围一圈彩带，需要多长？'),
        ('area','长方形面积','长方形长8厘米、宽3厘米，求占地面积。','用边长1厘米的小方块铺满长8厘米宽3厘米的卡片，要几块？'),
        ('order','整数混合运算','计算(24+6)×3，说明先算什么。','先求24与6的和，然后把所得结果乘3。'),
    ],
    'chinese': [
        ('metaphor','比喻修辞','识别句子中的比喻，把一种事物比作另一种事物。','这句把弯弯的月亮说成小船，这样表达有什么特点？'),
        ('personification','拟人修辞','判断把花草当作人来描写的表达方法。','小草向路过的人点头问好，这里怎样写出了小草的样子？'),
        ('hyperbole','夸张修辞','通过扩大或缩小事物的特点加强表达效果。','他饿得能吃下一头牛，为什么这样写而不是说很饿？'),
    ],
    'english': [
        ('present','一般现在时第三人称单数','Choose the correct verb: She ___ to school every day. (walk/walks)','He go to school every morning. Which word should change?'),
        ('past','一般过去时','Complete the sentence: Yesterday I ___ football. (play/played)','How should I say I visit my aunt last Sunday?'),
        ('comparative','形容词比较级','Choose the correct form: Tom is ___ than Jack. (tall/taller)','This box is big than that one. What is wrong with big?'),
    ],
    'physics': [
        ('ohm','欧姆定律','电阻两端电压6伏，电阻3欧，利用U=IR求电流。','阻值为3欧的导体接到6伏电源上，流过它的电流是多少？'),
        ('density','密度','物体质量20克、体积10立方厘米，求质量与体积的比值。','同一种材料每立方厘米有多重，该用哪两个测量结果计算？'),
        ('gravity','重力','已知质量为2千克，g取10牛每千克，求物体受到的重力。','2千克的物品被地球向下拉，g取10时这个力有多大？'),
    ],
    'chemistry': [
        ('fraction','溶质质量分数','把10克食盐溶于90克水，求食盐质量占溶液总质量的百分比。','糖水里有糖20克和水80克，糖占整杯糖水百分之几？'),
        ('conservation','质量守恒','化学反应前后参加反应的各物质总质量相等。','密闭容器中物质反应后，为什么称得的总质量不变？'),
        ('combustion','燃烧条件','可燃物燃烧需要接触氧气并达到着火点。','用杯子罩住蜡烛后火灭了，哪一个燃烧条件不再满足？'),
    ],
    'biology': [
        ('photosynthesis','光合作用','绿色植物在光照下利用二氧化碳和水制造有机物并释放氧气。','叶片在阳光下用空气里的二氧化碳制造养料，发生了什么过程？'),
        ('respiration','呼吸作用','细胞分解有机物释放能量以维持生命活动。','植物到了晚上还要消耗养料来获得能量，这叫什么？'),
        ('food_chain','食物链','根据生物的捕食关系，按被吃到吃的方向连接食物链。','草被蚱蜢吃，蚱蜢又被青蛙吃，应该怎样用箭头连接？'),
    ],
    'geography': [
        ('rotation','地球自转与昼夜更替','地球绕地轴旋转，使同一地点交替进入白昼和黑夜。','为什么我们这里白天过后会变成晚上？'),
        ('revolution','地球公转与季节','地球公转及地轴倾斜导致一年中的季节变化。','为什么一年中有春夏秋冬，而不是每天都一样？'),
        ('scale','地图比例尺','比例尺表示图上距离与实际距离的比值。','地图上两地相隔2厘米，要换算实际距离还需要什么信息？'),
    ],
    'history': [
        ('qin','秦朝巩固统一','秦统一后推行统一文字和度量衡等措施。','秦始皇为什么要求各地使用统一的文字和计量标准？'),
        ('silk_road','丝绸之路','古代连接中国与中亚、西亚等地区的商贸和文化交流通道。','古代中国的丝绸通过哪些陆上交流通道传到西方？'),
        ('industrial','工业革命','机器生产逐渐替代手工劳动，蒸汽动力推动工厂发展。','工厂用蒸汽动力机器代替手工生产，属于哪场生产变革？'),
    ],
}


def test_cross_subject_retrieval_uses_real_model_and_keeps_correct_targets(installed_models, monkeypatch, tmp_path):
    monkeypatch.setattr(socket.socket,'connect',lambda *a:pytest.fail('Local retrieval attempted network access'))
    matcher=LocalSemanticMatcher(installed_models)
    raw=[]
    def score(query,documents):
        result=matcher.scores(query,documents)
        raw.append(result)
        return result
    monkeypatch.setattr(document_independent_check,'original_version',lambda s:'synthetic-original')
    results=[]
    for subject, examples in BANK.items():
        sources={key:dict(diagnostic_item_id=key,knowledge_node_id=key,title=title,prompt=prompt,subject=subject)
                 for key,title,prompt,_ in examples}
        rows=[dict(diagnostic_item_id=key,prompt=value['prompt'],subject=subject) for key,value in sources.items()]
        connection=SimpleNamespace(execute=lambda *a:SimpleNamespace(fetchall=lambda:rows),
            local_semantic_matcher=SimpleNamespace(scores=score))
        monkeypatch.setattr(document_independent_check,'_qualified',lambda c,child,key:sources.get(key))
        for expected,_,_,query in [*examples,('unrelated','','','我今天放学想吃什么点心好呢？')]:
            packet=({'child_id':'synthetic-child'},{},{},{},{'confirmed_text':query},{'subject':subject})
            proposal=document_target_hypothesis.candidates(connection,packet)
            ranked=[v['node_id'] for v in proposal['options']] if proposal else []
            scores=raw[-1].get('scores',[])
            ids=list(sources)
            results.append(dict(subject=subject,expected=expected,ranked=ranked,
                dense_top=ids[scores.index(max(scores))] if scores else None,
                dense_scores=dict(zip(ids,scores)),
                passed=(not ranked if expected=='unrelated' else bool(ranked) and ranked[0]==expected)))
    (tmp_path/'subject-retrieval.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    failures=[v for v in results if not v['passed']]
    assert not failures,failures
