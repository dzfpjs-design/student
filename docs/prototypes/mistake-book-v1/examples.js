/* Synthetic worksheet and study data for the interface prototype. */
window.MISTAKE_DEMO = {
  "cases": [
    {
      "id": "math-fractions-1",
      "subject": "数学",
      "title": "分数除法 · 除数取倒数",
      "date": "2026-11-09",
      "cause": "运算规则",
      "typeId": "fraction-division",
      "stage": "待复习",
      "question": "12. 计算 3/4 ÷ 2/5，并写出最简结果。",
      "answer": "3/10",
      "correct": "15/8",
      "ocrStatus": "verified",
      "image": "assets/math-fractions-1.jpg",
      "analysis": {
        "summary": "这一步的关键是把除法转成乘法，再把除数 2/5 取倒数。原作答 3/10 恰好等于 3/4 × 2/5。",
        "causes": [
          {
            "label": "可能把除号直接改成乘号",
            "body": "原答案与两个分数直接相乘一致。可以回看草稿，确认当时有没有把 2/5 写成 5/2。"
          },
          {
            "label": "缺少大小核对",
            "body": "除以一个介于 0 和 1 之间的正数，结果应大于原数。3/10 比 3/4 小。"
          }
        ],
        "knowledge": [
          "分数除法",
          "倒数",
          "商的大小"
        ],
        "strategy": [
          "圈出除数 2/5，只把它改写成 5/2。",
          "算 3/4 × 5/2 = 15/8，再用结果乘回 2/5。"
        ]
      },
      "variant": {
        "prompt": "计算 5/6 ÷ 2/3。",
        "options": [
          {
            "key": "A",
            "text": "5/9"
          },
          {
            "key": "B",
            "text": "5/4"
          },
          {
            "key": "C",
            "text": "4/5"
          }
        ],
        "correct": "B",
        "explanation": "把除数 2/3 换成倒数 3/2，得到 5/6 × 3/2 = 5/4。",
        "hint": "先找出除号右边的数。"
      },
      "due": true
    },
    {
      "id": "physics-heat-1",
      "subject": "物理",
      "title": "勺柄变热 · 判断传热方式",
      "date": "2026-11-08",
      "cause": "概念混淆",
      "typeId": "heat-transfer",
      "stage": "待复习",
      "question": "6. 将一把冷金属勺放入热水，一会儿勺柄变热。改变勺柄内能的主要方式是什么？",
      "answer": "做功",
      "correct": "热传递",
      "ocrStatus": "verified",
      "image": "assets/physics-heat-1.jpg",
      "analysis": {
        "summary": "热水与勺子有温差，热量沿勺子传递，勺柄的内能增加。",
        "causes": [
          {
            "label": "可能混淆了内能变化与变化方式",
            "body": "勺柄变热说明内能增加。要再检查有没有温差和机械做功，才能判断原因。"
          }
        ],
        "knowledge": [
          "内能",
          "热传递",
          "做功改变内能"
        ],
        "strategy": [
          "找出高温与低温的物体。",
          "检查是否存在明显摩擦、压缩等机械作用。"
        ]
      },
      "variant": {
        "prompt": "手握装着热水的杯子，手逐渐变暖，主要方式是什么？",
        "options": [
          {
            "key": "A",
            "text": "热传递"
          },
          {
            "key": "B",
            "text": "做功"
          },
          {
            "key": "C",
            "text": "内能自行产生"
          }
        ],
        "correct": "A",
        "explanation": "杯子比手温度高，能量通过热传递进入手。",
        "hint": "先找温差，再找摩擦或压缩。"
      },
      "due": true
    },
    {
      "id": "math-fractions-2",
      "captureOnly": true,
      "subject": "数学",
      "title": "分数除法 · 辨认倒数对象",
      "date": "2026-11-07",
      "cause": "运算规则",
      "typeId": "fraction-division",
      "stage": "练习中",
      "question": "13. 计算 7/8 ÷ 7/12，并约分。",
      "answer": "2/3",
      "correct": "3/2",
      "ocrStatus": "verified",
      "image": "assets/math-fractions-2.jpg",
      "analysis": {
        "summary": "除法改写时，保留第一个分数，把第二个分数 7/12 倒过来。",
        "causes": [
          {
            "label": "可能把被除数取了倒数",
            "body": "2/3 等于 8/7 × 7/12。这个结果提示可以重点检查取倒数的是哪一个数。"
          }
        ],
        "knowledge": [
          "被除数与除数",
          "分数除法",
          "约分"
        ],
        "strategy": [
          "给除号两侧分别标上“被除数”和“除数”。",
          "改写为 7/8 × 12/7，再约分。"
        ]
      },
      "variant": {
        "prompt": "计算 2/3 ÷ 4/9。",
        "options": [
          {
            "key": "A",
            "text": "2/3"
          },
          {
            "key": "B",
            "text": "8/27"
          },
          {
            "key": "C",
            "text": "3/2"
          }
        ],
        "correct": "C",
        "explanation": "2/3 × 9/4 = 18/12 = 3/2。",
        "hint": "除号右边的 4/9 才是除数。"
      },
      "due": false
    },
    {
      "id": "english-tense-1",
      "subject": "英语",
      "title": "一般过去时 · yesterday",
      "date": "2026-11-06",
      "cause": "时态判断",
      "typeId": "past-vs-perfect",
      "stage": "待复习",
      "question": "8. 用括号中动词的适当形式填空。\nI _____ (see) the movie yesterday.",
      "answer": "have seen",
      "correct": "saw",
      "ocrStatus": "verified",
      "image": "assets/english-tense-1.jpg",
      "analysis": {
        "summary": "yesterday 给出明确的过去时间。这句问的是昨天发生的动作，用一般过去时。",
        "causes": [
          {
            "label": "可能忽略了时间状语",
            "body": "先标出 yesterday，再确定动词形式。see 的过去式是 saw。"
          }
        ],
        "knowledge": [
          "一般过去时",
          "过去时间状语",
          "不规则动词"
        ],
        "strategy": [
          "圈出 yesterday。",
          "把 see 变为 saw，并完整读句子。"
        ]
      },
      "variant": {
        "prompt": "We _____ (visit) the museum last Saturday.",
        "options": [
          {
            "key": "A",
            "text": "have visited"
          },
          {
            "key": "B",
            "text": "visited"
          },
          {
            "key": "C",
            "text": "visit"
          }
        ],
        "correct": "B",
        "explanation": "last Saturday 指明确结束的过去时间，用 visited。",
        "hint": "last Saturday 指上一个星期六。"
      },
      "due": true
    },
    {
      "id": "math-fractions-3",
      "subject": "数学",
      "title": "分数应用题 · 装满几杯",
      "date": "2026-11-05",
      "cause": "审题遗漏",
      "typeId": "fraction-division",
      "stage": "待核对",
      "question": "14. 有 5/6 升果汁，每杯装 1/4 升。最多能装满几杯？还剩多少升？",
      "answer": "10/3 杯",
      "correct": "3 杯，剩 1/12 升",
      "ocrStatus": "needs_review",
      "image": "assets/math-fractions-3.jpg",
      "analysis": {
        "summary": "相除可求总量相当于几杯，“装满”还需要取完整杯数，再计算余量。",
        "causes": [
          {
            "label": "可能漏读了“装满”",
            "body": "5/6 ÷ 1/4 = 10/3 这个商是正确的。题目要求完整杯数，不能把 1/3 杯计成装满。"
          },
          {
            "label": "原答案没有写余量",
            "body": "从总量中减去 3 × 1/4，可得 1/12 升。"
          }
        ],
        "knowledge": [
          "分数除法的意义",
          "整数杯数",
          "余量"
        ],
        "strategy": [
          "先求总量相当于多少杯。",
          "取能装满的杯数，再用总量减去已装量。"
        ]
      },
      "variant": {
        "prompt": "有 7/8 升水，每杯装 1/4 升。最多能装满几杯，还剩多少升？",
        "options": [
          {
            "key": "A",
            "text": "3 杯，剩 1/8 升"
          },
          {
            "key": "B",
            "text": "3 杯，剩 1/2 升"
          },
          {
            "key": "C",
            "text": "4 杯，剩 0 升"
          }
        ],
        "correct": "A",
        "explanation": "7/8 ÷ 1/4 = 7/2，能装满 3 杯。剩 7/8 − 3/4 = 1/8 升。",
        "hint": "商的小数部分表示未装满的杯数，剩余量仍以升为单位。"
      },
      "due": false
    },
    {
      "id": "physics-heat-2",
      "subject": "物理",
      "title": "热传递 · 热量流向",
      "date": "2026-11-04",
      "cause": "概念混淆",
      "typeId": "heat-transfer",
      "stage": "已订正",
      "question": "7. 把一杯热水放进冷水盆中。两者达到同温前，热量主要从哪里传向哪里？",
      "answer": "从冷水传向热水",
      "correct": "从热水传向冷水",
      "ocrStatus": "verified",
      "image": "assets/physics-heat-2.jpg",
      "analysis": {
        "summary": "自然热传递的方向由温度高低决定。热水放热，冷水吸热。",
        "causes": [
          {
            "label": "可能倒置了能量流向",
            "body": "可以在图上标注温度，再画出从高温物体到低温物体的箭头。"
          }
        ],
        "knowledge": [
          "温差",
          "吸热和放热",
          "热平衡"
        ],
        "strategy": [
          "分别标记热水与冷水的温度。",
          "用箭头表示热量从高温处流向低温处。"
        ]
      },
      "variant": {
        "prompt": "冰块放入室温的水里，达到同温前热量主要从哪里流向哪里？",
        "options": [
          {
            "key": "A",
            "text": "从冰块到水"
          },
          {
            "key": "B",
            "text": "从水到冰块"
          },
          {
            "key": "C",
            "text": "两个方向同样多"
          }
        ],
        "correct": "B",
        "explanation": "室温水的温度高于冰块，热量从水传向冰块。",
        "hint": "先比较两者初始温度。"
      },
      "due": false
    },
    {
      "id": "english-tense-2",
      "subject": "英语",
      "title": "现在完成时 · since",
      "date": "2026-11-03",
      "cause": "时态判断",
      "typeId": "past-vs-perfect",
      "stage": "已订正",
      "question": "9. 用括号中动词的适当形式填空。\nShe _____ (live) here since 2020, and she still lives here now.",
      "answer": "lived",
      "correct": "has lived",
      "ocrStatus": "verified",
      "image": "assets/english-tense-2.jpg",
      "analysis": {
        "summary": "since 2020 表明起点，句末又说明状态持续到现在，因此用现在完成时。",
        "causes": [
          {
            "label": "可能只看见过去起点",
            "body": "2020 是开始时间。still lives here now 提供了持续到现在的证据。"
          }
        ],
        "knowledge": [
          "现在完成时",
          "since 引导起点",
          "have / has + 过去分词"
        ],
        "strategy": [
          "连起来读 since 和 still lives here now。",
          "主语 She 搭配 has，live 的过去分词是 lived。"
        ]
      },
      "variant": {
        "prompt": "He _____ (work) here for five years, and he still works here.",
        "options": [
          {
            "key": "A",
            "text": "worked"
          },
          {
            "key": "B",
            "text": "works"
          },
          {
            "key": "C",
            "text": "has worked"
          }
        ],
        "correct": "C",
        "explanation": "工作从过去开始并持续到现在。用 has worked。",
        "hint": "留意 still works here。"
      },
      "due": false
    },
    {
      "id": "math-fractions-4",
      "subject": "数学",
      "title": "分数除法 · 除以整数",
      "date": "2026-11-02",
      "cause": "运算规则",
      "typeId": "fraction-division",
      "stage": "待复习",
      "question": "15. 计算 1/2 ÷ 3。",
      "answer": "3/2",
      "correct": "1/6",
      "ocrStatus": "verified",
      "image": "assets/math-fractions-4.jpg",
      "analysis": {
        "summary": "整数 3 也有倒数，可以写成 3/1。把 1/2 平均分成 3 份，每份应比 1/2 小。",
        "causes": [
          {
            "label": "可能沿用了整数乘法",
            "body": "3/2 等于 1/2 × 3，说明可以回看“平均分 3 份”的含义。"
          }
        ],
        "knowledge": [
          "整数的倒数",
          "分数除以整数",
          "平均分"
        ],
        "strategy": [
          "先把 3 写作 3/1。",
          "乘以 1/3，并用平均分检查大小。"
        ]
      },
      "variant": {
        "prompt": "计算 3/5 ÷ 2。",
        "options": [
          {
            "key": "A",
            "text": "3/10"
          },
          {
            "key": "B",
            "text": "6/5"
          },
          {
            "key": "C",
            "text": "5/6"
          }
        ],
        "correct": "A",
        "explanation": "3/5 × 1/2 = 3/10。",
        "hint": "先想：把 3/5 平均分成两份，每份比 3/5 大还是小？"
      },
      "due": true
    }
  ],
  "topics": [
    {
      "id": "fraction-division",
      "title": "分数除法",
      "subject": "数学",
      "description": "区分取倒数的对象、商的大小和“整份”应用题。",
      "steps": [
        {
          "title": "找出共同卡点",
          "body": "先在算式里圈出除数。观察两道同类错题，3/10 来自直接相乘，2/3 来自把被除数取倒数。它们需要同一个起手动作：先认清要除以谁。"
        },
        {
          "title": "理解为什么取倒数",
          "body": "3/4 ÷ 2/5 是在问 3/4 里面有多少个 2/5。把每份 2/5 放大到 1，要乘 5/2；总量也一起乘 5/2，因此商是 15/8。"
        },
        {
          "title": "用结果核对含义",
          "body": "除以小于 1 的正数，商会变大。遇到“最多装满几杯”，求出的商表示容量相当于多少杯，还要取完整杯数并另算余量。"
        }
      ],
      "example": "3/4 ÷ 2/5 = 3/4 × 5/2 = 15/8。用 15/8 × 2/5 = 3/4 反向核对。",
      "practice": {
        "prompt": "每段丝带长 2/3 米，5/4 米的丝带相当于多少段？",
        "options": [
          {
            "key": "A",
            "text": "5/6 段"
          },
          {
            "key": "B",
            "text": "15/8 段"
          },
          {
            "key": "C",
            "text": "8/15 段"
          }
        ],
        "correct": "B",
        "explanation": "用总长度除以每段长度，5/4 ÷ 2/3 = 5/4 × 3/2 = 15/8。",
        "hint": "“每段”对应除数 2/3。"
      }
    },
    {
      "id": "heat-transfer",
      "title": "内能变化与热传递",
      "subject": "物理",
      "description": "先判断改变内能的方式，再确定热量的方向。",
      "steps": [
        {
          "title": "观察能量交换的条件",
          "body": "勺子和热水、杯子和手都有温差。先给两个物体标出相对温度，再核对有没有压缩、摩擦等机械作用。"
        },
        {
          "title": "区分两种改变内能的方式",
          "body": "热传递以温差为条件，机械做功可以在没有温差驱动时改变内能。比如搓手变暖，主要是摩擦做功。"
        },
        {
          "title": "画出热量方向",
          "body": "自然热传递从高温处指向低温处。热水降温，冷水升温，达到同温后没有净传热。"
        }
      ],
      "example": "热水让冷金属勺变热，热量由热水经勺身传到勺柄。",
      "practice": {
        "prompt": "冬天快速搓手发热，主要通过什么方式增加手的内能？",
        "options": [
          {
            "key": "A",
            "text": "太阳照射"
          },
          {
            "key": "B",
            "text": "机械做功"
          },
          {
            "key": "C",
            "text": "冷空气传热"
          }
        ],
        "correct": "B",
        "explanation": "双手摩擦，机械能转化为内能。",
        "hint": "搓手时发生了什么机械作用？"
      }
    },
    {
      "id": "past-vs-perfect",
      "title": "一般过去时与现在完成时",
      "subject": "英语",
      "description": "从时间线判断事件已结束，还是状态延续到现在。",
      "steps": [
        {
          "title": "定位时间词",
          "body": "yesterday 和 last Saturday 指一个已经结束的过去时间。since 2020 指动作或状态的起点，需要检查是否延续到现在。"
        },
        {
          "title": "把句子放在时间线上",
          "body": "I saw it yesterday 把事件放在昨天。She has lived here since 2020 把居住状态从 2020 年连到现在。"
        },
        {
          "title": "迁移到新的句子",
          "body": "先标时间词，再核对句子其他部分。最后检查动词过去式或 have / has + 过去分词的构成。"
        }
      ],
      "example": "I saw the movie yesterday. / She has lived here since 2020.",
      "practice": {
        "prompt": "He _____ (finish) the book two days ago.",
        "options": [
          {
            "key": "A",
            "text": "has finished"
          },
          {
            "key": "B",
            "text": "finished"
          },
          {
            "key": "C",
            "text": "finishes"
          }
        ],
        "correct": "B",
        "explanation": "two days ago 表示明确过去时间，用 finished。",
        "hint": "ago 指从现在往前的时间点。"
      }
    }
  ],
  "capture": {
    "image": "assets/capture-sheet.jpg",
    "regions": [
      {
        "caseId": "math-fractions-1",
        "x": 7.5758,
        "y": 15.1587,
        "w": 84.8485,
        "h": 38.7302
      },
      {
        "caseId": "math-fractions-2",
        "x": 7.5758,
        "y": 56.0317,
        "w": 84.8485,
        "h": 38.7302
      }
    ]
  }
};
