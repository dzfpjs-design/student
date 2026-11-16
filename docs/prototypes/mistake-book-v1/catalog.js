/* Sample course profiles and local search for the prototype. Course lists are illustrative. */
(() => {
  'use strict';

  const elementary = ['语文', '数学', '英语', '科学', '道德与法治'];
  const junior = ['语文', '数学', '英语', '道德与法治', '历史', '地理', '生物'];
  const courses = {
    六年级: elementary,
    七年级: junior,
    八年级: [...junior, '物理'],
    九年级: ['语文', '数学', '英语', '道德与法治', '历史', '物理', '化学'],
  };
  const gradeAliases = {
    6: '六年级', 六: '六年级', 小六: '六年级',
    7: '七年级', 七: '七年级', 初一: '七年级', 初中一年级: '七年级',
    8: '八年级', 八: '八年级', 初二: '八年级', 初中二年级: '八年级',
    9: '九年级', 九: '九年级', 初三: '九年级', 初中三年级: '九年级',
  };
  const termsByConcept = [
    ['分数除法', '分数相除', '分数做除法', '除以分数'],
    ['倒数', '取倒数', '乘倒数', '颠倒分子分母'],
    ['热传递', '传热', '热量传递', '热量流向'],
    ['一般过去时', '过去式', '过去时', 'yesterday'],
    ['现在完成时', '完成时', 'since'],
    ['运算规则', '运算法则', '计算错', '算错'],
    ['审题遗漏', '漏看条件', '看漏条件', '没看清题'],
    ['概念混淆', '概念搞混', '概念分不清'],
    ['英语', '英文'],
  ];

  function canonicalGrade(value) {
    const grade = String(value ?? '').trim().replace(/^小学/, '');
    const alias = grade.replace(/^(\d+)年级$/, '$1');
    return Object.hasOwn(gradeAliases, alias) ? gradeAliases[alias] : grade;
  }

  function recordGrade(record) {
    return canonicalGrade(record.grade || record.gradeLabel || record.grade_label) || (record.subject === '数学' ? '六年级' : '八年级');
  }

  function gradeSubjects(grade, records = []) {
    const activeGrade = canonicalGrade(grade);
    const seen = records.filter(record => !activeGrade || recordGrade(record) === activeGrade).map(record => record.subject);
    const profileCourses = Object.hasOwn(courses, activeGrade) ? courses[activeGrade] : [];
    return [...new Set([...profileCourses, ...seen].filter(Boolean))];
  }

  function recordTags(record) {
    const tags = Array.isArray(record.tags) ? record.tags : Array.isArray(record.causeTags) ? record.causeTags : [record.cause];
    return [...new Set(tags.map(tag => typeof tag === 'string' ? tag.trim() : String(tag?.label ?? '').trim()).filter(Boolean))];
  }

  function topicName(record) {
    return record.typeLabel || window.MISTAKE_DEMO?.topics.find(topic => topic.id === record.typeId)?.title || '';
  }

  function normalize(value) {
    return String(value ?? '').normalize('NFKC').toLocaleLowerCase('zh-CN').trim();
  }

  function queryTerms(query) {
    const text = normalize(query)
      .replace(/^(?:小知[，,\s]*)?(?:(?:请)?(?:帮我|给我)?(?:找一下|查一下|搜一下|找找|看看|搜索|查找|查|找))?/, '')
      .replace(/(?:近\s*[7七]\s*天|最近一周|最近|上周|今天|刚才|以前)(?:的)?/g, ' ')
      .replace(/(?:错题|题目|那道题|这道题|的题|做错的|错过的|里面|一下)(?:的)?/g, ' ')
      .replace(/^(?:是|的|有)\s*|(?:吧|吗|的)[。?!]?$/g, '');
    const qualifiers = [...new Set([...elementary, ...junior, ...courses.九年级, '英文'])];
    const separated = qualifiers.reduce((result, qualifier) => result.replaceAll(qualifier, ` ${qualifier} `), text);
    return separated.split(/[\s、,。；;·:!?]+/).map(term => term.replace(/^的|的$/g, '').replace(/^(.{2,})题$/, '$1')).filter(Boolean);
  }

  function fields(record) {
    return [record.title, record.question, record.subject, topicName(record), ...recordTags(record),
      ...(record.analysis?.knowledge || [])].map(normalize).filter(Boolean);
  }

  function matchScore(text, term) {
    const equivalents = termsByConcept.find(terms => terms.includes(term)) || [term];
    if (equivalents.includes(text)) return 6;
    if (equivalents.some(equivalent => text.startsWith(equivalent))) return 4;
    return equivalents.some(equivalent => text.includes(equivalent)) ? 2 : 0;
  }

  function findMatches(record, query) {
    const searchable = fields(record);
    return searchable.includes(normalize(query)) || queryTerms(query).every(term => searchable.some(field => matchScore(field, term) > 0));
  }

  function suggestions(records, query) {
    const terms = queryTerms(query);
    if (!terms.length) return [];
    const matches = records.filter(record => findMatches(record, query));
    const candidates = [
      ...matches.map(record => ({ kind: 'case', label: record.title, query: record.title, id: record.id })),
      ...[...new Set(matches.map(topicName).filter(Boolean))].map(label => ({ kind: 'topic', label, query: label })),
      ...[...new Set(matches.flatMap(recordTags))].map(label => ({ kind: 'cause', label, query: label })),
    ];
    const score = ({ label }) => {
      const text = normalize(label);
      return terms.reduce((sum, term) => sum + matchScore(text, term), 0);
    };
    const caps = { case: 3, topic: 2, cause: 2 };
    return candidates.sort((left, right) => score(right) - score(left)).filter(candidate => caps[candidate.kind]-- > 0).slice(0, 6);
  }

  window.MistakePrototypeCatalog = Object.freeze({ gradeSubjects, recordGrade, recordTags, findMatches, suggestions });
})();
