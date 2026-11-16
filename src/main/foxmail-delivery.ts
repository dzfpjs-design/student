import { spawn } from 'node:child_process'

export type FoxmailMessage = {
  recipient: string
  subject: string
  body: string
}

export type FoxmailDeliveryReceipt = {
  accepted: true
  client: string
  transport: 'simple_mapi'
}

type ProcessResult = { exitCode: number; stdout: string; stderr: string }
export type MailProcessRunner = (encodedCommand: string, input: string) => Promise<ProcessResult>

export async function sendWithConfiguredFoxmail(
  message: FoxmailMessage,
  runner: MailProcessRunner = runPowerShell,
): Promise<FoxmailDeliveryReceipt> {
  const normalized = validateMessage(message)
  const encodedCommand = Buffer.from(FOXMAIL_MAPI_SCRIPT, 'utf16le').toString('base64')
  const encodedPayload = Buffer.from(JSON.stringify(normalized), 'utf8').toString('base64')
  const result = await runner(encodedCommand, encodedPayload)
  let receipt: { accepted?: boolean; client?: string; error_code?: string }
  try { receipt = JSON.parse(result.stdout.trim()) as typeof receipt } catch {
    throw new Error(result.exitCode === 0 ? 'FOXMAIL_MAPI_INVALID_RECEIPT' : 'FOXMAIL_MAPI_PROCESS_FAILED')
  }
  if (!receipt.accepted) throw new Error(receipt.error_code || 'FOXMAIL_MAPI_REJECTED')
  if (result.exitCode !== 0) throw new Error('FOXMAIL_MAPI_PROCESS_FAILED')
  return { accepted: true, client: receipt.client || 'Foxmail', transport: 'simple_mapi' }
}

function validateMessage(message: FoxmailMessage): FoxmailMessage {
  const recipient = message.recipient.trim()
  const subject = message.subject.trim()
  const body = message.body.trim()
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(recipient) || recipient.length > 320) throw new Error('FOXMAIL_RECIPIENT_INVALID')
  if (!subject || subject.length > 180) throw new Error('FOXMAIL_SUBJECT_INVALID')
  if (!body || body.length > 200_000) throw new Error('FOXMAIL_BODY_INVALID')
  return { recipient, subject, body }
}

function runPowerShell(encodedCommand: string, input: string): Promise<ProcessResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(resolveFoxmailPowerShell(process.platform, process.arch, process.env.SystemRoot), ['-NoProfile', '-NonInteractive', '-EncodedCommand', encodedCommand], {
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    const stdout: Buffer[] = []
    const stderr: Buffer[] = []
    let timedOut = false
    const timeout = setTimeout(() => { timedOut = true; child.kill() }, 30_000)
    child.stdout.on('data', (chunk: Buffer) => stdout.push(chunk))
    child.stderr.on('data', (chunk: Buffer) => stderr.push(chunk))
    child.once('error', (error) => { clearTimeout(timeout); reject(error) })
    child.once('close', (exitCode) => {
      clearTimeout(timeout)
      if (timedOut) { reject(new Error('FOXMAIL_MAPI_TIMEOUT')); return }
      resolve({ exitCode: exitCode ?? -1, stdout: Buffer.concat(stdout).toString('utf8'), stderr: Buffer.concat(stderr).toString('utf8') })
    })
    child.stdin.end(input, 'utf8')
  })
}

export function resolveFoxmailPowerShell(platform: NodeJS.Platform, arch: string, systemRoot?: string): string {
  if (platform === 'win32' && arch === 'x64' && systemRoot?.trim()) {
    return `${systemRoot.replace(/[\\/]+$/, '')}\\SysWOW64\\WindowsPowerShell\\v1.0\\powershell.exe`
  }
  return 'powershell.exe'
}

// Payload is read from stdin so child data never appears in the process list.
export const FOXMAIL_MAPI_SCRIPT = String.raw`
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$encodedPayload = [Console]::In.ReadToEnd().Trim()
$payloadText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encodedPayload))
$payload = $payloadText | ConvertFrom-Json
$client = ''
foreach ($path in @('HKCU:\Software\Clients\Mail', 'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Clients\Mail')) {
  try {
    $value = (Get-Item $path -ErrorAction Stop).GetValue('')
    if ($value) { $client = [string]$value; break }
  } catch {}
}
if ($client -notmatch 'Foxmail') {
  @{ accepted = $false; error_code = 'FOXMAIL_NOT_DEFAULT'; client = $client } | ConvertTo-Json -Compress
  exit 12
}
$source = @'
using System;
using System.Runtime.InteropServices;
public static class HscSimpleMapi {
  public static string LastStage = "send";
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Ansi)]
  struct MapiMessage { public int reserved; public string subject; public string noteText; public string messageType; public string dateReceived; public string conversationID; public int flags; public IntPtr originator; public int recipCount; public IntPtr recips; public int fileCount; public IntPtr files; }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Ansi)]
  struct MapiRecipDesc { public int reserved; public int recipClass; public string name; public string address; public int entryIDSize; public IntPtr entryID; }
  [DllImport("MAPI32.DLL", CharSet=CharSet.Ansi)]
  static extern int MAPILogon(IntPtr uiParam, string profileName, string password, int flags, int reserved, out IntPtr session);
  [DllImport("MAPI32.DLL")]
  static extern int MAPILogoff(IntPtr session, IntPtr uiParam, int flags, int reserved);
  [DllImport("MAPI32.DLL", CharSet=CharSet.Ansi)]
  static extern int MAPISendMail(IntPtr session, IntPtr uiParam, MapiMessage message, int flags, int reserved);
  public static int Send(string recipient, string subject, string body) {
    var target = new MapiRecipDesc { recipClass = 1, name = recipient, address = "SMTP:" + recipient };
    int size = Marshal.SizeOf(typeof(MapiRecipDesc));
    IntPtr pointer = Marshal.AllocHGlobal(size);
    try {
      Marshal.StructureToPtr(target, pointer, false);
      var message = new MapiMessage { subject = subject, noteText = body, recipCount = 1, recips = pointer };
      IntPtr session;
      LastStage = "logon";
      int logonCode = MAPILogon(IntPtr.Zero, null, null, 64 | 2, 0, out session);
      if (logonCode != 0) {
        // Foxmail 7.x can expose Simple MAPI sending without implementing
        // MAPILogon for a non-interactive caller. A zero session is explicitly
        // supported by MAPISendMail and still keeps the submission dialog-free.
        LastStage = "send_without_session";
        return MAPISendMail(IntPtr.Zero, IntPtr.Zero, message, 0, 0);
      }
      LastStage = "send";
      try { return MAPISendMail(session, IntPtr.Zero, message, 0, 0); }
      finally { MAPILogoff(session, IntPtr.Zero, 0, 0); }
    } finally { Marshal.DestroyStructure(pointer, typeof(MapiRecipDesc)); Marshal.FreeHGlobal(pointer); }
  }
}
'@
Add-Type -TypeDefinition $source -Language CSharp
$code = [HscSimpleMapi]::Send([string]$payload.recipient, [string]$payload.subject, [string]$payload.body)
if ($code -ne 0) {
  $codeText = if ($code -lt 0) { ('0x{0:X8}' -f ($code -band 0xffffffff)) } else { [string]$code }
  $stage = [HscSimpleMapi]::LastStage.ToUpperInvariant()
  @{ accepted = $false; error_code = ('FOXMAIL_MAPI_' + $stage + '_' + $codeText); client = $client } | ConvertTo-Json -Compress
  exit 13
}
@{ accepted = $true; client = $client } | ConvertTo-Json -Compress
`
