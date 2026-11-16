from PyInstaller.utils.hooks import copy_metadata


# The import name is ``webrtcvad``, while the maintained wheel distribution is
# named ``webrtcvad-wheels``. The upstream contrib hook still asks for metadata
# under the import name and fails before analysis starts on Windows.
datas = copy_metadata("webrtcvad-wheels")
hiddenimports = ["_webrtcvad"]
