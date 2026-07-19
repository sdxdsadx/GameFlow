$defaultUser = '26142328@qq.com'
$user = Read-Host "发件 QQ 邮箱（直接回车使用 $defaultUser）"
if ([string]::IsNullOrWhiteSpace($user)) { $user = $defaultUser }
$secure = Read-Host '请输入 QQ 邮箱 SMTP 授权码（输入内容不会显示）' -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $authCode = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    if ([string]::IsNullOrWhiteSpace($authCode)) { throw '授权码不能为空' }
    [Environment]::SetEnvironmentVariable('GAMEFLOW_SMTP_USER', $user, 'User')
    [Environment]::SetEnvironmentVariable('GAMEFLOW_SMTP_AUTH_CODE', $authCode, 'User')
    Write-Host '邮箱配置已保存。请关闭并重新打开 GameFlow 控制面板。' -ForegroundColor Green
} finally {
    if ($pointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
    $authCode = $null
}
Read-Host '按回车键关闭'
