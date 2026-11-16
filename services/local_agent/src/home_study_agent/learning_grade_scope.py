"""Learning eligibility includes earlier school foundations, not higher grades.

This is a curriculum constraint only; callers still check child ownership,
source validity, review authority and independent assessment evidence.
"""

SCHOOL_GRADES = ('一年级', '二年级', '三年级', '四年级', '五年级', '六年级', '初一', '初二', '初三')


def allows_learning_grade(child_grade: str, grade_band: str, track: str) -> bool:
    if not child_grade or not grade_band:
        return False
    if grade_band.startswith(child_grade):
        return True
    return (track == 'school' and child_grade in SCHOOL_GRADES and grade_band in SCHOOL_GRADES
            and SCHOOL_GRADES.index(grade_band) < SCHOOL_GRADES.index(child_grade))
