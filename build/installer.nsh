!macro customUnInstall
  # The old uninstaller is also invoked during an in-place update. Preserve
  # the family's existing auto-launch preference and initialization marker.
  ${ifNot} ${isUpdated}
    ExecWait '"$INSTDIR\${APP_EXECUTABLE_FILENAME}" --remove-auto-launch'
  ${endif}
!macroend
