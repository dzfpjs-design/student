"""Explicit offline practice rendering of a validated original stimulus.

Never called for child speech or arbitrary HTTP text. This is not an original
exam recording or a calibrated listening assessment. Parent audition is required.
"""

import base64
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import wave


_WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$text = [Console]::In.ReadToEnd()
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$stream = New-Object System.IO.MemoryStream
try {
  $voice = $s.GetInstalledVoices() | Where-Object {
    $_.Enabled -and $_.VoiceInfo.Name -in @('Microsoft David Desktop','Microsoft Zira Desktop','Microsoft Hazel Desktop')
  } | Select-Object -First 1
  if (!$voice) { throw 'OFFLINE_ENGLISH_VOICE_UNAVAILABLE' }
  $s.SelectVoice($voice.VoiceInfo.Name)
  $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
  $s.SetOutputToAudioStream($stream, $format)
  $s.Speak($text)
  $s.SetOutputToNull()
  [Console]::Out.Write([Convert]::ToBase64String($stream.ToArray()))
} finally { $s.Dispose(); $stream.Dispose() }
"""


def render_original_practice(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 3000:
        raise ValueError("original_stimulus_length_unsupported")
    try:
        if sys.platform == "darwin":
            # Only explicitly authorized, source-validated stimulus reaches here. The transient
            # platform output is removed before encrypted durable storage.
            with tempfile.TemporaryDirectory(prefix="hsc-original-practice-") as temporary:
                path = Path(temporary) / "practice.wav"
                subprocess.run(["/usr/bin/say", "-v", "Daniel", "-o", str(path),
                    "--file-format=WAVE", "--data-format=LEI16@16000"], input=text.encode(),
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=45)
                content = path.read_bytes()
            engine = "macos_installed_daniel_v1"
        elif sys.platform == "win32":
            shell = Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            encoded = base64.b64encode(_WINDOWS_SCRIPT.encode("utf-16-le")).decode("ascii")
            result = subprocess.run([str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                input=text.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=45)
            pcm = base64.b64decode(result.stdout.strip(), validate=True)
            if not pcm or len(pcm) % 2 or len(pcm) > 24 * 1024 * 1024 - 76:
                raise ValueError("offline_renderer_invalid_pcm")
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as audio:
                audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                audio.writeframes(pcm)
            content = buffer.getvalue()
            engine = "windows_installed_system_speech_v1"
        else:
            raise ValueError("offline_original_renderer_unavailable")
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("offline_original_renderer_unavailable") from exc
    if not content.startswith(b"RIFF") or len(content) > 24 * 1024 * 1024 - 32:
        raise ValueError("offline_renderer_invalid_pcm")
    return content, engine
