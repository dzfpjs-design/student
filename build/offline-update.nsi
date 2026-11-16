Unicode true
RequestExecutionLevel user
SetCompressor /SOLID lzma
!include "MUI2.nsh"
!ifndef HELPER_EXE
  !error "HELPER_EXE is required"
!endif
!ifndef MANIFEST_FILE
  !error "MANIFEST_FILE is required"
!endif
!ifndef ASAR_FILE
  !error "ASAR_FILE is required"
!endif
!ifndef AGENT_FILE
  !error "AGENT_FILE is required"
!endif
!ifndef OUTPUT_EXE
  !error "OUTPUT_EXE is required"
!endif
Name "家庭学习伙伴离线更新"
OutFile "${OUTPUT_EXE}"
BrandingText "家庭学习伙伴 · 本机离线更新"
ShowInstDetails show
!define MUI_WELCOMEPAGE_TITLE "家庭学习伙伴离线更新"
!define MUI_WELCOMEPAGE_TEXT "本更新仅适用于当前 Windows 用户已有的家庭学习伙伴。$\r$\n$\r$\n请先保存孩子和家长的工作。更新器会请求应用正常退出，再在本机备份数据库和加密配置。资料、媒体和草稿保留原位。$\r$\n$\r$\n更新无需网络、Node 或 Python。应用未能正常退出时会停止更新，不会强制结束进程。请不要关闭更新器。"
!define MUI_FINISHPAGE_TITLE "程序更新已确认"
!define MUI_FINISHPAGE_TEXT "程序版本已校验。请通过原桌面图标启动家庭学习伙伴。$\r$\n$\r$\n本次未恢复旧数据库，也未删除资料或草稿。本机备份和安装日志保存在当前用户 LocalAppData 的 home-study-companion-updates 目录。"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "SimpChinese"

Section "安全更新"
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=hsc-offline-update-helper.exe "${HELPER_EXE}"
  SetOutPath "$PLUGINSDIR\payload"
  File /oname=manifest.json "${MANIFEST_FILE}"
  File /oname=app.asar "${ASAR_FILE}"
  File /oname=home-study-local-agent.exe "${AGENT_FILE}"
  DetailPrint "正在校验版本、正常退出应用、备份并更新；正常退出最多等待两分钟。"
  ; ExecWait observes the helper's natural exit. No stock installer process checks.
  ExecWait '"$PLUGINSDIR\hsc-offline-update-helper.exe" --manifest "$PLUGINSDIR\payload\manifest.json" --payload-dir "$PLUGINSDIR\payload"' $0
  IfErrors launch_failed
  StrCmp $0 0 update_ok
  DetailPrint "更新未完成。请保留本机安装日志；程序没有自动恢复旧数据库。"
  SetErrorLevel $0
  Abort
launch_failed:
  MessageBox MB_OK|MB_ICONSTOP "更新工具未能启动。未执行应用更新，请保留此安装包并联系维护者。"
  SetErrorLevel 30
  Abort
update_ok:
  DetailPrint "程序和版本校验已完成；请从原桌面图标启动。"
  SetErrorLevel 0
SectionEnd
