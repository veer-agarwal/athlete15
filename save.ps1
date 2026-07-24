param([Parameter(Mandatory=$true)][string]$Message)

git add -A
git commit -m $Message
if ($LASTEXITCODE -ne 0) {
    Write-Host "nothing to commit"
    exit 0
}
git push
