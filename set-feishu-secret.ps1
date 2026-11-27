$secureSecret = Read-Host "Enter the regenerated Feishu App Secret" -AsSecureString
$secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureSecret)
try {
    $plainSecret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    [Environment]::SetEnvironmentVariable("FEISHU_APP_SECRET", $plainSecret, "User")
    Write-Host "FEISHU_APP_SECRET was saved for the current Windows user. Restart Codex before using the MCP."
}
finally {
    if ($secretPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
    }
}
