# Tillegal credential protection. Data travels on stdin/stdout, never argv or disk in plaintext.
param([Parameter(Mandatory=$true)][ValidateSet('protect','unprotect')][string]$Operation)
$ErrorActionPreference = 'Stop'
try {
    Add-Type -AssemblyName System.Security
    $payload = [Convert]::FromBase64String([Console]::In.ReadToEnd().Trim())
    $scope = [System.Security.Cryptography.DataProtectionScope]::CurrentUser
    $entropy = [System.Text.Encoding]::UTF8.GetBytes('tillegal-judge-chatgpt-v1')
    if ($Operation -eq 'protect') {
        $result = [System.Security.Cryptography.ProtectedData]::Protect($payload, $entropy, $scope)
    } else {
        $result = [System.Security.Cryptography.ProtectedData]::Unprotect($payload, $entropy, $scope)
    }
    [Console]::Out.Write([Convert]::ToBase64String($result))
} catch {
    [Console]::Error.Write('CHATGPT_SECURE_STORAGE_ERROR')
    exit 1
}
