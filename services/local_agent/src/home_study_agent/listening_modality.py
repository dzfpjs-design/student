"""Classify the current database node; this never grants source or playback authority."""


def school_listening_node(node):
    return (node['track'] == 'school' and node['code'] == 'J3.ENGLISH.LISTENING'
            and node['subject'] == '英语' and node['grade_band'] == '初三')


def listening_node(node, answer_type=None):
    return (school_listening_node(node) or (node['track'] == 'pet'
            and (node['code'].upper().startswith('PET.LISTENING.') or answer_type == 'listening_text')))


def listening_source_matches_node(source, node):
    if source['schema'] in {'school_listening_source_v1','school_listening_source_v2'}:
        return (school_listening_node(node) and source['subject'] == node['subject']
                and source['grade'] == node['grade_band']
                and source['knowledge_node_id'] == node['knowledge_node_id'])
    return (source['schema'] in {'pet_listening_source_v1','pet_listening_source_v2'} and node['track'] == 'pet'
            and node['code'].startswith('PET.LISTENING.'))
