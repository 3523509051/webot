param(
    [string]$WechatRoot,
    [string]$BridgeDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) 'bridge')
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

try {
    Write-Host '请先停止微信桥，在微信“设置 → 账号与存储”查看数据根目录。'
    Write-Host '填写 xwechat_files 这一层，不填具体账号子目录或 db_storage。'
    if ([string]::IsNullOrWhiteSpace($WechatRoot)) {
        $WechatRoot = Read-Host '请输入微信数据根目录（可粘贴带引号的路径）'
    }
    $WechatRoot = $WechatRoot.Trim().Trim('"')
    if ([string]::IsNullOrWhiteSpace($WechatRoot) -or
        !(Test-Path -LiteralPath $WechatRoot -PathType Container)) {
        throw '目录不存在或输入为空，配置未修改。'
    }
    $WechatRoot = (Resolve-Path -LiteralPath $WechatRoot).ProviderPath
    $accounts = @(Get-ChildItem -LiteralPath $WechatRoot -Directory | Where-Object {
        Test-Path -LiteralPath (Join-Path $_.FullName 'db_storage') -PathType Container
    })
    if ($accounts.Count -eq 0) {
        throw '未找到账号数据库。请先登录微信，并确认这是账号子目录的上一级。'
    }
    $bridge = (Resolve-Path -LiteralPath $BridgeDirectory).ProviderPath
    $target = Join-Path $bridge 'config.json'
    $exists = Test-Path -LiteralPath $target -PathType Leaf
    $source = if ($exists) { $target } else { Join-Path $bridge 'config.example.json' }
    $original = [IO.File]::ReadAllText($source)
    $config = $original | ConvertFrom-Json
    if ($config -isnot [PSCustomObject]) { throw '配置必须是 JSON 对象，未修改文件。' }
    if ($config.PSObject.Properties.Name -notcontains 'wechat') {
        $config | Add-Member -NotePropertyName 'wechat' -NotePropertyValue ([PSCustomObject]@{})
    }
    if ($config.wechat -isnot [PSCustomObject]) { throw 'wechat 配置不是 JSON 对象，未修改文件。' }
    $config.wechat | Add-Member -NotePropertyName 'db_dir' -NotePropertyValue $WechatRoot -Force
    $json = $config | ConvertTo-Json -Depth 100
    $suffix = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8)
    $temporary = Join-Path $bridge ('.config-write-' + $suffix + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, $json, (New-Object System.Text.UTF8Encoding($false)))
        if ($exists) {
            if ([IO.File]::ReadAllText($target) -cne $original) { throw '配置在操作期间已被修改，请重试。' }
            $backup = $target + '.bak-' + $suffix
            [IO.File]::Replace($temporary, $target, $backup)
            Write-Host ('原配置备份：' + $backup)
        } else {
            [IO.File]::Move($temporary, $target)
        }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
    }
    Write-Host ('已保存微信数据根目录：' + $WechatRoot)
    Write-Host '其他配置保留；此操作尚未绑定账号，也不会启动服务或发送消息。'
    Write-Host '下一步：只登录机器人微信 → 绑定微信账号.cmd → start.cmd。'
    exit 0
} catch {
    $safeErrors = @(
        '目录不存在或输入为空，配置未修改。',
        '未找到账号数据库。请先登录微信，并确认这是账号子目录的上一级。',
        '配置必须是 JSON 对象，未修改文件。',
        'wechat 配置不是 JSON 对象，未修改文件。',
        '配置在操作期间已被修改，请重试。'
    )
    $detail = if ($_.Exception.Message -in $safeErrors) { $_.Exception.Message } else {
        '无法读取或写入配置，请检查 JSON 格式及文件权限。未输出配置内容。'
    }
    Write-Host ('配置失败：' + $detail) -ForegroundColor Red
    exit 1
}
