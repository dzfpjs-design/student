/* Local fixtures for the prototype's paper and voice answer paths. */
(() => {
  const answers = {
    'math-fractions-1': {
      paper: ['15/8', '3/10'],
      voice: [
        '除数是五分之二，取倒数得到二分之五，再乘四分之三，结果是八分之十五。',
        '我把除号改成乘号，直接乘两个分数，结果是十分之三。',
      ],
      alternate: [['八分之十五', '1又7/8', '一又八分之七'], ['十分之三']],
      fraction: true,
      prompt: '先找出除数。把除法改写成乘法时，哪个数需要改变？',
    },
    'math-fractions-2': {
      paper: ['3/2', '2/3'],
      voice: ['把十二分之七取倒数，再乘八分之七，约分后是二分之三。', '我把第一个分数倒过来，约分后是三分之二。'],
      alternate: [['二分之三', '1又1/2', '一又二分之一'], ['三分之二']],
      fraction: true,
      prompt: '先指出除号右边的数，想想哪一个分数应该取倒数。',
    },
    'math-fractions-3': {
      paper: ['3 杯，剩 1/12 升', '10/3 杯'],
      voice: ['能装满三杯，装完剩十二分之一升。', '最多能装三分之十杯，这就是我的答案。'],
      alternate: [['3杯剩余1/12升', '三杯剩十二分之一升', '三杯还剩十二分之一升'], ['三分之十杯']],
      prompt: '题目同时问了完整杯数和余量。你能先指出哪一部分的杯子还没装满吗？',
    },
    'math-fractions-4': {
      paper: ['1/6', '3/2'],
      voice: ['把二分之一平均分成三份，每份是六分之一。', '二分之一乘三，得到二分之三。'],
      alternate: [['六分之一'], ['二分之三']],
      fraction: true,
      prompt: '把二分之一平均分成三份，每份会变大还是变小？',
    },
    'physics-heat-1': {
      paper: ['热传递', '做功'],
      voice: ['热水和勺子有温差，热量通过勺子传到勺柄，所以是热传递。', '勺柄变热是因为热水对勺子做功。'],
      alternate: [['热传导', '传热', '热量从热水传到勺柄'], ['热水做功']],
      prompt: '勺子和水之间有温差吗？这里有明显的摩擦或者压缩吗？',
    },
    'physics-heat-2': {
      paper: ['从热水传向冷水', '从冷水传向热水'],
      voice: ['热水温度更高，热量从热水传到冷水。', '热量应该从冷水传到热水。'],
      alternate: [['从热水到冷水', '热水→冷水', '热水传向冷水', '从高温到低温'], ['从冷水到热水', '冷水→热水', '冷水传向热水', '从低温到高温']],
      prompt: '先标出两杯水的温度，想想哪一杯会放出热量。',
    },
    'english-tense-1': {
      paper: ['saw', 'have seen'],
      voice: ['填 saw。Yesterday 是明确的过去时间，这里用一般过去时。', '填 have seen，我想用现在完成时。'],
      alternate: [['I saw the movie yesterday', '填saw', 'see的过去式saw'], ['I have seen the movie yesterday', '填have seen', 'see', 'seen']],
      prompt: '先找出句子的时间词。它指一个已经结束的时间，还是延续到了现在？',
    },
    'english-tense-2': {
      paper: ['has lived', 'lived'],
      voice: ['填 has lived。她从二零二零年起住在这里，现在还住在这里。', '填 lived，因为二零二零年是过去的时间。'],
      alternate: [['She has lived here since 2020', '填has lived'], ['She lived here since 2020', '填lived', 'have lived', 'lives']],
      prompt: '把 since 和句末的 still 连起来读，这件事现在还在持续吗？',
    },
  };

  const unrecognized = { paper: '（草稿拍糊，只看得到半行）', voice: '（背景声音遮住了最终答案）' };
  function entryFor(record) {
    const id = String(record?.id ?? '').replace(/^capture-/, '');
    return Object.hasOwn(answers, id) ? answers[id] : undefined;
  }

  function getExamples(record) {
    const entry = entryFor(record);
    return Object.fromEntries(['paper', 'voice'].map(method => [method, {
      correct: entry?.[method][0] ?? '',
      incorrect: entry?.[method][1] ?? '',
      unclear: unrecognized[method],
    }]));
  }

  function normalize(text) {
    return String(text ?? '').normalize('NFKC').toLowerCase()
      .replace(/^\s*(?:我的答案|答案|结果)\s*(?:是|[:：])\s*[:：]?\s*/, '')
      .replaceAll('⁄', '/').replace(/\s*\/\s*/g, '/')
      .replace(/[，、,。!！?？；;：:"“”]/g, ' ').replace(/\.\s*$/, '')
      .replace(/\s+/g, ' ').trim();
  }

  function fractionValue(text) {
    if (/^-?\d{1,6}\.\d{1,6}$/.test(text)) return Number(text);
    const match = /^(-?\d{1,6})(?:\/(\d{1,6}))?$/.exec(text);
    if (!match) return null;
    const denominator = Number(match[2] ?? 1);
    return denominator > 0 ? Number(match[1]) / denominator : null;
  }

  function assess(record, method, text) {
    const entry = entryFor(record);
    if (method !== 'paper' && method !== 'voice') {
      return { outcome: 'unclear', feedback: '先选择草稿纸或语音作答。' };
    }
    const normalized = normalize(text);
    const unclear = {
      outcome: 'unclear',
      feedback: method === 'paper'
        ? '这次还无法确认作答。把草稿摆正后再试，或补充最后一行。'
        : '这次还无法确认作答。请再说一遍你的结论，或补充关键一步。',
    };
    if (!entry || !normalized) return unclear;

    const matches = index => [entry.paper[index], entry.voice[index], ...entry.alternate[index]]
      .some(candidate => normalize(candidate) === normalized);
    if (matches(0)) {
      return { outcome: 'correct', feedback: '这次独立做对了。过一段时间再复测一次。' };
    }
    if (matches(1)) {
      return { outcome: 'incorrect', feedback: entry.prompt };
    }

    if (entry.fraction) {
      const value = fractionValue(normalized);
      if (value !== null && value !== fractionValue(entry.paper[0])) {
        return { outcome: 'incorrect', feedback: entry.prompt };
      }
      if (value !== null) {
        return { outcome: 'unclear', feedback: '这个数值等价。请再写出最简结果。' };
      }
    }
    return unclear;
  }

  window.MistakePrototypeReview = { getExamples, assess };
})();
