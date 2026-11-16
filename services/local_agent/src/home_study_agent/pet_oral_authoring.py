"""Local parent form descriptors; no upload, model, or scoring permission."""
from . import pet_oral_source as oral
from .local_supply import LocalSupplyError, validate_item


def authoring_options(c, child_id):
    child = c.execute("SELECT grade FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()
    if not child:
        raise KeyError(child_id)
    options = []
    for family, (code, answer_type) in oral.FAMILIES.items():
        for row in c.execute('''SELECT di.diagnostic_item_id,di.knowledge_node_id,kn.title
            FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)
            WHERE kn.track='pet' AND kn.subject='英语' AND kn.grade_band=? AND kn.code=?
              AND kn.status='active' AND di.status='active' AND di.answer_type=?
            ORDER BY di.diagnostic_item_id''', (child['grade'] + '-PET', code, answer_type)):
            try:
                validate_item(c, child_id, row['diagnostic_item_id'])
                picture = oral.bind_stimulus(c, child_id, row['diagnostic_item_id']) if family == 'picture_description' else None
                if picture and picture['stimulus_asset'] != 'pet-study-room':
                    continue
            except LocalSupplyError:
                continue
            options.append(dict(task_family=family, knowledge_node_id=row['knowledge_node_id'],
                seed_item_id=row['diagnostic_item_id'], title=row['title'], answer_type=answer_type,
                stimulus_binding=picture))
            break
    return dict(child_id=child_id, grade=child['grade'], options=options,
                local_only=True, cloud_transmission_allowed=False)
