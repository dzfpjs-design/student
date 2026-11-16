"""Versioned closed-answer rules. A rule never manufactures seed provenance."""
from fractions import Fraction
import json


TEMPLATES = (
    {"template_id": "p4_arithmetic_v1", "grade": "四年级", "subject": "数学", "scope": "school",
     "nodes": ("node-p4-math-arithmetic",), "auto_qualification": True},
    {"template_id": "p4_fraction_decimal_v1", "grade": "四年级", "subject": "数学", "scope": "school",
     "nodes": ("node-p4-math-fractions-decimals",), "auto_qualification": True},
    {"template_id": "p4_distributive_v1", "grade": "四年级", "subject": "数学", "scope": "competition_math",
     "nodes": ("node-oly-arithmetic-structure",), "auto_qualification": True},
    {"template_id": "pet_narrow_meaning_v1", "grade": "四年级", "subject": "英语", "scope": "pet",
     "nodes": ("node-pet-reading-vocabulary",), "auto_qualification": True},
    {"template_id": "pet_present_simple_v1", "grade": "四年级", "subject": "英语", "scope": "pet",
     "nodes": ("node-pet-writing-grammar", "node-pet-grammar-sentence"), "auto_qualification": False},
)


def closed_template(seed, stage, ordinal):
    """Return a recomputable template result, or None for an unrelated node."""
    template = next((row for row in TEMPLATES if seed["knowledge_node_id"] in row["nodes"]), None)
    if template is None:
        return None
    if stage not in {"parallel", "near_transfer", "discrimination"}:
        raise ValueError("LOCAL_STAGE_UNSUPPORTED")
    if not isinstance(ordinal, int) or not 0 <= ordinal < 36:
        raise ValueError("LOCAL_TEMPLATE_EXHAUSTED")
    family, n = template["template_id"], ordinal + 3
    answer_format = "token"
    proof = {"template_version": family, "ordinal": ordinal}
    if family == "p4_arithmetic_v1":
        a, b, c = 20 + n, n, 2 + ordinal % 5
        if stage == "discrimination":
            prompt = f"计算 {a}+{b}×{c}，先乘后加。只回答一个整数。"
            value = a + b * c
        else:
            prompt = f"计算 ({a}+{b})×{c}，只回答一个整数。"
            value = (a + b) * c
        answer = str(value)
        explanation = f"按括号和乘加运算顺序逐步计算，结果为{answer}。"
        proof.update(a=a, b=b, c=c, value=value)
    elif family == "p4_fraction_decimal_v1":
        # Work in tenths/hundredths and like denominators; no float rounding.
        numerator = ordinal % 8 + 1
        if stage == "discrimination":
            left, right = Fraction(numerator, 10), Fraction(numerator + 1, 100)
            prompt = f"比较 {numerator}/10 和 0.{numerator + 1:02d}：A 左边大；B 右边大；C 相等。只回答字母。"
            answer = "A" if left > right else "B" if right > left else "C"
            explanation = f"化成同分母：{numerator * 10}/100 与 {numerator + 1}/100 比较，所以选{answer}。"
        else:
            left, right = Fraction(numerator, 10), Fraction(ordinal % 4 + 1, 10)
            prompt = f"计算 {left.numerator}/{left.denominator}+{right.numerator}/{right.denominator}，回答分数或小数。"
            answer = str(left + right)
            answer_format = "rational"
            explanation = f"精确通分相加后得到{answer}。"
        proof.update(left=str(left), right=str(right))
    elif family == "p4_distributive_v1":
        a, b = 90 + n, 10 + n
        prompt = f"用分配律计算 {a}×{b}，只回答一个整数。"
        answer = str(a * b)
        explanation = f"{a}×{b}=({a + 1}-1)×{b}={a + 1}×{b}-{b}={answer}。"
        proof.update(a=a, b=b, product=a * b)
        if stage == "discrimination":
            prompt = f"{a}×{b} 应改写为哪项？A：({a + 1}×{b})-{b}；B：({a + 1}×{b})-1。只回答字母。"
            answer = "A"
    elif family == "pet_narrow_meaning_v1":
        contexts = ("path", "doorway", "corridor", "bridge", "passage", "walkway")
        if ordinal >= len(contexts):
            raise ValueError("LOCAL_TEMPLATE_EXHAUSTED")
        context = contexts[ordinal]
        if "not wide" not in json.loads(seed["expected_answer_json"]).get("keywords", []):
            raise ValueError("LOCAL_SEED_MEANING_UNVERIFIED")
        prompt = f"Read: 'The {context} was narrow, so only one person could pass at a time.' What does narrow mean? A: not wide; B: very noisy; C: very expensive. Answer A, B or C."
        answer = "A"
        explanation = "The source meaning of narrow is 'not wide'; answer A."
        if stage == "discrimination":
            prompt = f"The {context} is narrow, not wide. Which word has the opposite meaning to narrow? A: quiet; B: wide; C: short. Answer A, B or C."
            answer = "B"
            explanation = "Wide is the opposite of narrow; answer B."
        proof.update(lexeme="narrow", source_meaning="not wide", context=context)
    else:
        subjects = (("She", "plays"), ("He", "plays"), ("They", "play"), ("We", "play"), ("I", "play"), ("The students", "play"))
        if ordinal >= len(subjects):
            raise ValueError("LOCAL_TEMPLATE_EXHAUSTED")
        subject, verb = subjects[ordinal]
        prompt = f"Choose the present simple form: '{subject} ___ tennis every Sunday.' A: play; B: plays. Answer A or B."
        answer = "A" if verb == "play" else "B"
        explanation = f"With {subject} in this affirmative present-simple sentence, use {verb}."
        proof.update(subject=subject, verb=verb, source_is_open_rubric=True)
        if stage == "discrimination":
            auxiliary = "does not" if verb == "plays" else "do not"
            prompt = f"Choose the present simple form: '{subject} {auxiliary} ___ tennis on Sundays.' A: play; B: plays. Answer A or B."
            answer = "A"
            explanation = f"After {auxiliary}, use the base form play, not plays."
            proof.update(auxiliary=auxiliary, verb="play")
    return {"prompt": prompt, "explanation": explanation, "answer": answer,
            "answer_format": answer_format, "proof": proof, "auto_qualification": template["auto_qualification"]}


def readiness(connection, child_id, qualified):
    child = connection.execute("SELECT grade FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()
    if not child:
        raise ValueError("UNKNOWN_CHILD")
    result = []
    for template in TEMPLATES:
        if child[0] != template["grade"]:
            continue
        seeds = [row["diagnostic_item_id"] for row in qualified if row["knowledge_node_id"] in template["nodes"]]
        result.append({"template_id": template["template_id"], "subject": template["subject"], "scope": template["scope"],
            "seed_item_ids": seeds, "status": "requires_reviewed_seed" if not seeds else
            "requires_closed_answer_seed" if not template["auto_qualification"] else "ready",
            "auto_qualification_allowed": bool(seeds and template["auto_qualification"]),
            "does_not_modify_builtin_catalog": True})
    return result
