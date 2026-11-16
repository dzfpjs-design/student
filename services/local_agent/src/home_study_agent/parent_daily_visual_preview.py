"""Parent-only local preview; no snapshot, model call, or external delivery."""
from .agent_runtime import _RuntimeTransactionDatabase
from .daily_page_observation import daily_page_observation
from .daily_observation_projection import project_daily_observation
from .daily_visual_evidence import project_daily_visual_evidence
from .daily_study_duration import project_recorded_task_time


def parent_daily_visual_preview(database, child_id: str, day: str) -> dict:
    """Read both projections in one SQLite snapshot, not separately at different times."""
    with database.connect() as connection:
        connection.execute('BEGIN')
        try:
            view = _RuntimeTransactionDatabase(connection)
            visual = project_daily_visual_evidence(view, child_id, day)
            timing = daily_page_observation(view, child_id, day)
            observations = project_daily_observation(connection, child_id, day)
            task_time = project_recorded_task_time(connection, child_id, day)
            return {'child_id': child_id, 'learning_date': day, 'timezone': 'Asia/Shanghai',
                    'document_observation': visual, 'page_observation_time': timing,
                    'camera_and_document_samples': observations,
                    'recorded_task_time': {'tasks': [{k:v for k,v in task.items() if k != 'plan_item_id'} for task in task_time['tasks']],
                                           'recorded_active_seconds': task_time['recorded_active_seconds'],
                                           'omitted_count': task_time['omitted_count']},
                    'local_only': True, 'cloud_transmission_allowed': False,
                    'is_formal_report': False,
                    'limitations': ['OCR 文字需要核对，看到页面不代表学习或掌握。',
                        '页面可见时间仅为符合采样条件的估计，不代表做题、背书或专注时长。']}
        finally:
            connection.rollback()
