# Functions only. Importing this file does not access a device or any archive.
Set-StrictMode -Version Latest
if (-not ('HscAsarNative' -as [type])) {
 Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class HscAsarNative {
 [StructLayout(LayoutKind.Sequential)] struct INFO {
   public uint attributes; public System.Runtime.InteropServices.ComTypes.FILETIME created, accessed, written;
   public uint volume, sizeHigh, sizeLow, links, indexHigh, indexLow;
 }
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern SafeFileHandle CreateFileW(string name,uint access,uint share,IntPtr security,uint creation,uint flags,IntPtr template);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetFileInformationByHandle(SafeFileHandle h,out INFO info);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern uint GetFinalPathNameByHandleW(SafeFileHandle h,StringBuilder path,uint length,uint flags);
 public static string FinalPath(SafeFileHandle h) {
   var b=new StringBuilder(32768);uint n=GetFinalPathNameByHandleW(h,b,(uint)b.Capacity,0);
   if(n==0 || n>=b.Capacity || !b.ToString().StartsWith(@"\\?\",StringComparison.Ordinal) || b.ToString().StartsWith(@"\\?\UNC\",StringComparison.OrdinalIgnoreCase))throw new Exception("FINAL_PATH_UNSUPPORTED");
   return b.ToString().Substring(4);
 }
 public static SafeFileHandle HoldFile(string path) {
   // GENERIC_READ, share read only; FILE_FLAG_OPEN_REPARSE_POINT and sequential scan.
   var h=CreateFileW(path,0x80000000,1,IntPtr.Zero,3,0x08200000,IntPtr.Zero);
   if(h.IsInvalid){h.Dispose();throw new Exception("FILE_OPEN_FAILED");}
   INFO i;if(!GetFileInformationByHandle(h,out i) || (i.attributes&0x400)!=0 || (i.attributes&0x10)!=0 || i.links!=1){h.Dispose();throw new Exception("FILE_IDENTITY_INVALID");}
   return h;
 }
 public static SafeFileHandle HoldDirectory(string path) {
   // FILE_READ_ATTRIBUTES, share read/write but deny deletion; open the reparse point itself.
   var h=CreateFileW(path,0x80,3,IntPtr.Zero,3,0x02200000,IntPtr.Zero);
   if(h.IsInvalid){h.Dispose();throw new Exception("DIRECTORY_OPEN_FAILED");}
   INFO i; if(!GetFileInformationByHandle(h,out i) || (i.attributes&0x400)!=0 || (i.attributes&0x10)==0){h.Dispose();throw new Exception("DIRECTORY_REPARSE_OR_INVALID");}
   return h;
 }
 public static string FileIdentity(SafeFileHandle h) {
   INFO i;if(!GetFileInformationByHandle(h,out i) || (i.attributes&0x400)!=0 || (i.attributes&0x10)!=0 || i.links!=1)throw new Exception("FILE_IDENTITY_INVALID");
   return i.volume+":"+i.indexHigh+":"+i.indexLow+":"+i.sizeHigh+":"+i.sizeLow+":"+i.written.dwHighDateTime+":"+i.written.dwLowDateTime;
 }
}
'@
}
function Get-StreamHash([IO.Stream]$Stream){
 $Stream.Position=0;$sha=[Security.Cryptography.SHA256]::Create()
 try{return [BitConverter]::ToString($sha.ComputeHash($Stream)).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
}
function Read-Exact([IO.Stream]$Stream,[int]$Size){
 if($Size -lt 0){throw 'READ_SIZE_INVALID'}
 $b=New-Object byte[] $Size;$n=0
 while($n -lt $Size){$r=$Stream.Read($b,$n,$Size-$n);if($r -le 0){throw 'TRUNCATED_INPUT'};$n+=$r}
 return ,$b
}
function Assert-UniqueJsonKeys([string]$Text){
 $stack=New-Object 'System.Collections.Generic.Stack[object]';$i=0;$count=0
 while($i -lt $Text.Length){
  $c=$Text[$i]
  if($c -eq '"'){
   $start=$i;$i++;$closed=$false
   while($i -lt $Text.Length){
    if($Text[$i] -eq '\'){$i+=2;continue}
    if($Text[$i] -eq '"'){$i++;$closed=$true;break}
    $i++
   }
   if(-not $closed){throw 'JSON_STRING_INVALID'}
   $end=$i;$j=$i
   while($j -lt $Text.Length -and [char]::IsWhiteSpace($Text[$j])){$j++}
   if($j -lt $Text.Length -and $Text[$j] -eq ':'){
    if($stack.Count -eq 0 -or $null -eq $stack.Peek()){throw 'JSON_KEY_CONTEXT_INVALID'}
    $count++;if($count -gt 50000){throw 'JSON_KEYS_LIMIT'}
    $literal=$Text.Substring($start,$end-$start)
    $decoded=ConvertFrom-Json -InputObject ('{"key":'+$literal+'}') -ErrorAction Stop
    if($decoded.key -isnot [string]){throw 'JSON_KEY_INVALID'}
    if(-not $stack.Peek().Add($decoded.key)){throw 'JSON_DUPLICATE_KEY'}
   }
   continue
  }
  if($c -eq '{'){$stack.Push((New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)))}
  elseif($c -eq '['){$stack.Push($null)}
  elseif($c -eq '}' -or $c -eq ']'){if($stack.Count -eq 0){throw 'JSON_DEPTH_INVALID'};[void]$stack.Pop()}
  if($stack.Count -gt 64){throw 'JSON_DEPTH_LIMIT'}
  $i++
 }
 if($stack.Count -ne 0){throw 'JSON_DEPTH_INVALID'}
}
function Read-HscAsarEvidence([string]$Path,[string]$ExpectedSha){
 if(-not [BitConverter]::IsLittleEndian -or $ExpectedSha -cnotmatch '^[a-f0-9]{64}$'){throw 'INPUT_INVALID'}
 if($Path -notmatch '^[A-Za-z]:\\' -or $Path.Substring(2).Contains(':')){throw 'PATH_UNSUPPORTED'}
 $absolute=[IO.Path]::GetFullPath($Path)
 if(-not [IO.Path]::IsPathRooted($absolute) -or $absolute.StartsWith('\\')){throw 'PATH_UNSUPPORTED'}
 $directory=[IO.Path]::GetDirectoryName($absolute);$held=New-Object 'System.Collections.Generic.List[Microsoft.Win32.SafeHandles.SafeFileHandle]';$file=$null
 try {
  $ancestors=New-Object 'System.Collections.Generic.List[string]';$ancestor=$directory
  while($ancestor){$ancestors.Add($ancestor);$ancestor=[IO.Path]::GetDirectoryName($ancestor)}
  $ancestors.Reverse()
  foreach($ancestor in $ancestors){
   $h=[HscAsarNative]::HoldDirectory($ancestor);$held.Add($h)
   if([HscAsarNative]::FinalPath($h).TrimEnd('\') -ine $ancestor.TrimEnd('\')){throw 'DIRECTORY_IDENTITY_MISMATCH'}
  }
  $fh=[HscAsarNative]::HoldFile($absolute)
  try {
   if([HscAsarNative]::FinalPath($fh) -ine $absolute){throw 'FILE_PATH_MISMATCH'}
   $file=New-Object IO.FileStream($fh,[IO.FileAccess]::Read)
  }catch{$fh.Dispose();throw}
  $identity=[HscAsarNative]::FileIdentity($file.SafeFileHandle)
  if($file.Length -lt 16 -or $file.Length -gt 1073741824){throw 'ARCHIVE_SIZE_INVALID'}
  $first=Get-StreamHash $file;if($first -cne $ExpectedSha){throw 'ARCHIVE_HASH_MISMATCH'}
  $file.Position=0;$pre=Read-Exact $file 16
  $payload=[BitConverter]::ToUInt32($pre,0);$head=[BitConverter]::ToUInt32($pre,4);$pickle=[BitConverter]::ToUInt32($pre,8);$jsonSize=[BitConverter]::ToUInt32($pre,12)
  if($payload -ne 4 -or $head -lt 8 -or $head -gt 8388608 -or $head % 4 -ne 0 -or $head -gt ($file.Length-8) -or $pickle -ne ($head-4) -or $jsonSize -gt ($pickle-4)){throw 'HEADER_INVALID'}
  $jsonBytes=Read-Exact $file ([int]$jsonSize);$utf8=New-Object Text.UTF8Encoding($false,$true);$json=$utf8.GetString($jsonBytes)
  Assert-UniqueJsonKeys $json
  $header=ConvertFrom-Json -InputObject $json -ErrorAction Stop
  $entry=$header.files.out.files.main.files.'index.js'
  if(-not $entry -or $entry.PSObject.Properties['unpacked'] -or $entry.PSObject.Properties['link']){throw 'ENTRY_UNSUPPORTED'}
  [uint64]$offset=0;[long]$size=0
  if(-not [uint64]::TryParse([string]$entry.offset,[ref]$offset) -or -not [long]::TryParse([string]$entry.size,[ref]$size)){throw 'ENTRY_SIZE_INVALID'}
  $base=[uint64](8+$head)
  if($size -lt 0 -or $size -gt 20971520 -or $base -gt [uint64]$file.Length -or $offset -gt ([uint64]$file.Length-$base)){throw 'ENTRY_BOUNDS_INVALID'}
  $start=$base+$offset;if([uint64]$size -gt ([uint64]$file.Length-$start)){throw 'ENTRY_BOUNDS_INVALID'}
  $file.Position=[long]$start;$module=Read-Exact $file ([int]$size)
  $source=$utf8.GetString($module);$sha=[Security.Cryptography.SHA256]::Create()
  try{$moduleSha=[BitConverter]::ToString($sha.ComputeHash($module)).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
  $firstIdentity=[HscAsarNative]::FileIdentity($file.SafeFileHandle)
  $last=Get-StreamHash $file
  $check=[HscAsarNative]::HoldFile($absolute)
  try{if([HscAsarNative]::FinalPath($check) -ine $absolute){throw 'FILE_PATH_MISMATCH'};$pathIdentity=[HscAsarNative]::FileIdentity($check)}finally{$check.Dispose()}
  if($identity -cne $firstIdentity -or $identity -cne $pathIdentity -or $first -cne $last){throw 'ARCHIVE_CHANGED'}
  return @{module_sha256=$moduleSha;marker_api=$source.Contains('encryptedApiKey');marker_smtp=$source.Contains('encryptedSmtpPassword')}
 } finally {if($file){$file.Dispose()};foreach($h in $held){$h.Dispose()}}
}
