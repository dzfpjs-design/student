import { describe, expect, it } from 'vitest'

import { sendWithConfiguredFoxmail } from './foxmail-delivery'

const recipient = process.env.HSC_FOXMAIL_LIVE_RECIPIENT?.trim()

describe('Foxmail live delivery', () => {
  it.runIf(process.platform === 'win32' && Boolean(recipient))(
    'sends a family-data-free installation receipt through the configured Foxmail client',
    async () => {
      const sentAt = new Date().toISOString()
      const receipt = await sendWithConfiguredFoxmail({
        recipient: recipient!,
        subject: `家庭学习伙伴安装验证 ${sentAt}`,
        body: [
          '这是一封家庭学习伙伴部署阶段的自动发送验证邮件。',
          `发送时间：${sentAt}`,
          '此邮件不包含孩子档案、学习记录、音频、视频、图片或设备文件路径。',
        ].join('\n'),
      })

      expect(receipt).toMatchObject({ accepted: true, transport: 'simple_mapi' })
    },
    45_000,
  )
})
