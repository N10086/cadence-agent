# SP1 完整验收冒烟 —— 覆盖 FR-1 ~ FR-12 全部 12 条需求
#
# 响应格式：统一外壳 {code, message, data}（v0.2.0 起）
#   - 成功：$r.Json.code -eq 0，业务数据在 $r.Json.data
#   - 失败：$r.Json.code 为 HTTP 状态码（400/403/404/409/422/500），无 data
#
# 用法：后端跑起来后执行  .\smoke.ps1

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Net.Http

$base = 'http://localhost:18080/api/v1'
$client = [System.Net.Http.HttpClient]::new()
$client.DefaultRequestHeaders.Add('X-Actor', 'agent')

function Send($method, $url, $obj, $headers) {
    $req = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::new($method), $url)
    if ($null -ne $obj) {
        $json = $obj | ConvertTo-Json -Compress -Depth 6
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
        $req.Content = [System.Net.Http.ByteArrayContent]::new($bytes)
        $req.Content.Headers.ContentType =
            [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse('application/json; charset=utf-8')
    }
    if ($headers) { $headers.GetEnumerator() | ForEach-Object { $req.Headers.TryAddWithoutValidation($_.Key, $_.Value) | Out-Null } }
    $resp = $client.SendAsync($req).GetAwaiter().GetResult()
    $text = $resp.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    [pscustomobject]@{
        Status  = [int]$resp.StatusCode
        Body    = $text
        Json    = $(if ($text) { try { $text | ConvertFrom-Json } catch { $null } } else { $null })
        Headers = $resp.Headers
    }
}

$script:pass = 0; $script:fail = 0
function Check($name, $cond, $detail) {
    if ($cond) { "  [PASS] $name"; $script:pass++ }
    else       { "  [FAIL] $name  -> $detail"; $script:fail++ }
}
function Q($iso) { return $iso -replace '\+', '%2B' }

# 全部业务数据都从 data 里取
function Data($r) { return $r.Json.data }

function New-Task($title, $extra) {
    $body = @{ title = $title }
    if ($extra) { $extra.GetEnumerator() | ForEach-Object { $body[$_.Key] = $_.Value } }
    $r = Send 'POST' "$base/tasks" $body $null
    if ($r.Status -ne 201 -or $r.Json.code -ne 0) { throw "创建失败: $title -> $($r.Status) $($r.Body)" }
    return $r.Json.data.id
}

# ============================================================
Write-Host "`n===== 0  统一响应外壳 =====" -ForegroundColor Cyan
$r = Send 'GET' "$base/tasks" $null $null
Check '成功响应 code=0' ($r.Json.code -eq 0) $r.Json.code
Check '成功响应 message=success' ($r.Json.message -eq 'success') $r.Json.message
Check '业务数据在 data 下' ($null -ne $r.Json.data.items) 'missing data.items'

# ============================================================
Write-Host "`n===== FR-1/4  CRUD 与软删除 =====" -ForegroundColor Cyan
$id = New-Task '交季度报告' @{
    priority = 'URGENT'; dueAt = '2026-11-07T18:00:00+08:00'
    estimateMin = 360; tags = @('work')
}
$r = Send 'GET' "$base/tasks/$id" $null $null
Check '创建后可查' ($r.Status -eq 200 -and $r.Json.code -eq 0) $r.Status
Check '中文与 +08:00 保住' ($r.Json.data.task.title -eq '交季度报告' -and $r.Json.data.task.dueAt -like '*+08:00') $r.Json.data.task.dueAt

# ============================================================
Write-Host "`n===== FR-5  乐观锁 =====" -ForegroundColor Cyan
$r = Send 'PATCH' "$base/tasks/$id" @{ version = 999; title = 'x' } $null
Check '409 + code=409' ($r.Status -eq 409 -and $r.Json.code -eq 409) "$($r.Status)/$($r.Json.code)"
Check '失败响应无 data' ($null -eq $r.Json.data) $r.Body

# ============================================================
Write-Host "`n===== FR-2  状态机 =====" -ForegroundColor Cyan
$sm = New-Task '生命周期' @{}
$r = Send 'POST' "$base/tasks/${sm}:start" @{ version = 0 } $null
Check 'TODO → DOING' ($r.Status -eq 200 -and $r.Json.data.status -eq 'DOING') $r.Json.data.status
$r = Send 'POST' "$base/tasks/${sm}:complete" @{ version = 1 } $null
Check 'DOING → DONE' ($r.Status -eq 200 -and $r.Json.data.status -eq 'DONE') $r.Json.data.status
$r = Send 'POST' "$base/tasks/${sm}:start" @{ version = 2 } $null
Check 'DONE → start 被拒 422' ($r.Status -eq 422 -and $r.Json.code -eq 422) "$($r.Status)/$($r.Json.code)"
Check 'message 说明原因' ($r.Json.message -like '*TODO*') $r.Json.message
$r = Send 'POST' "$base/tasks/${sm}:reopen" @{ version = 2 } $null
Check 'DONE → reopen 回到 TODO' ($r.Status -eq 200 -and $r.Json.data.status -eq 'TODO') $r.Json.data.status
$r = Send 'POST' "$base/tasks/${sm}:cancel" @{ version = 3 } $null
Check 'TODO → CANCELLED' ($r.Status -eq 200 -and $r.Json.data.status -eq 'CANCELLED') $r.Json.data.status

# ============================================================
Write-Host "`n===== FR-7  依赖与环检测 =====" -ForegroundColor Cyan
$pre = New-Task '前置' @{}; $post = New-Task '后继' @{}
Send 'POST' "$base/tasks/$post/dependencies" @{ dependsOnId = $pre } $null | Out-Null
$r = Send 'POST' "$base/tasks/${post}:complete" @{ version = 0 } $null
Check '前置未完成 → 422' ($r.Status -eq 422 -and $r.Json.code -eq 422) "$($r.Status)/$($r.Json.code)"
Send 'POST' "$base/tasks/${pre}:complete" @{ version = 0 } $null | Out-Null
$r = Send 'POST' "$base/tasks/${post}:complete" @{ version = 0 } $null
Check '前置完成后可完成' ($r.Status -eq 200) $r.Status

$c1 = New-Task '环A' @{}; $c2 = New-Task '环B' @{}
Send 'POST' "$base/tasks/$c1/dependencies" @{ dependsOnId = $c2 } $null | Out-Null
$r = Send 'POST' "$base/tasks/$c2/dependencies" @{ dependsOnId = $c1 } $null
Check '成环 → 422' ($r.Status -eq 422) $r.Status

# ============================================================
Write-Host "`n===== FR-10  幂等创建 =====" -ForegroundColor Cyan
$key = 'idem-smoke-' + [guid]::NewGuid().ToString()
$r1 = Send 'POST' "$base/tasks" @{ title = '幂等任务' } @{ 'X-Idempotency-Key' = $key }
$r2 = Send 'POST' "$base/tasks" @{ title = '幂等任务' } @{ 'X-Idempotency-Key' = $key }
Check '两次请求返回同一 id' ($r1.Json.data.id -eq $r2.Json.data.id) "$($r1.Json.data.id) vs $($r2.Json.data.id)"
Check '重放标记存在' ($r2.Headers.Contains('Idempotent-Replayed')) $r2.Headers
$all = Send 'GET' "$base/tasks?keyword=$(Q '幂等任务')" $null $null
Check '只创建了 1 条' ($all.Json.data.total -eq 1) $all.Json.data.total

# ============================================================
Write-Host "`n===== FR-8  冲突警告（不阻止写入）=====" -ForegroundColor Cyan
$long = New-Task '长任务' @{ startAt = '2026-11-25T09:00:00+08:00'; estimateMin = 480 }
$short = New-Task '短任务' @{ startAt = '2026-11-25T18:00:00+08:00'; estimateMin = 30 }
$r = Send 'PATCH' "$base/tasks/$short" @{ version = 0; startAt = '2026-11-25T12:00:00+08:00' } $null
Check '写入成功 200' ($r.Status -eq 200) $r.Status
Check '返回 warnings' ($r.Json.data.warnings.Count -eq 1) $r.Json.data.warnings.Count
Check 'warning 码为 TIME_CONFLICT' ($r.Json.data.warnings[0].code -eq 'TIME_CONFLICT') $r.Json.data.warnings[0].code

# ============================================================
Write-Host "`n===== FR-6  重复任务 =====" -ForegroundColor Cyan
$rec = New-Task '每周站会' @{
    startAt = '2026-11-04T09:00:00+08:00'
    rrule = 'FREQ=WEEKLY;BYDAY=WE;COUNT=3'; estimateMin = 30
}
$r = Send 'GET' "$base/tasks/$rec/occurrences?from=$(Q '2026-11-01T00:00:00+08:00')&to=$(Q '2026-12-01T00:00:00+08:00')" $null $null
Check '展开 3 次' ($r.Json.data.occurrences.Count -eq 3) "$($r.Status) $($r.Body)"

# ============================================================
Write-Host "`n===== FR-9  忙闲 =====" -ForegroundColor Cyan
$r = Send 'GET' "$base/calendar/busy?from=$(Q '2026-11-04T00:00:00+08:00')&to=$(Q '2026-11-05T00:00:00+08:00')" $null $null
Check '返回忙碌区间' ($r.Status -eq 200 -and $r.Json.data.windows.Count -ge 1) "$($r.Status) $($r.Body)"

# ============================================================
Write-Host "`n===== FR-11  审计 =====" -ForegroundColor Cyan
$au = New-Task '审计任务' @{}
$r = Send 'PATCH' "$base/tasks/$au" @{ version = 0; title = '改过' } @{ 'X-Trace-Id' = 'tr_smoke' }
$r = Send 'GET' "$base/tasks/$au/history" $null $null
Check '历史含 2 条' ($r.Json.data.Count -eq 2) $r.Json.data.Count
Check '最新是 UPDATE' ($r.Json.data[0].action -eq 'UPDATE') $r.Json.data[0].action
Check 'traceId 已记录' ($r.Json.data[0].traceId -eq 'tr_smoke') $r.Json.data[0].traceId
Check 'before/after 都存在' ($null -ne $r.Json.data[0].before -and $null -ne $r.Json.data[0].after) 'missing'

# ============================================================
Write-Host "`n===== FR-4  删除两阶段确认 =====" -ForegroundColor Cyan
$del = New-Task '要删的' @{}
$r = Send 'DELETE' "$base/tasks/$del" $null $null
Check '无令牌 → 403 code=403' ($r.Status -eq 403 -and $r.Json.code -eq 403) "$($r.Status)/$($r.Json.code)"
$token = $r.Json.data.confirmToken
Check '令牌在 data.confirmToken' ($token -like 'ct_*') $token
Check 'data 里带任务快照' ($r.Json.data.snapshot.title -eq '要删的') $r.Json.data.snapshot.title
$still = Send 'GET' "$base/tasks/$del" $null $null
Check '此时任务还在' ($still.Status -eq 200) $still.Status
$r = Send 'DELETE' "$base/tasks/$del" $null @{ 'X-Confirm-Token' = $token }
Check '带令牌 → 204' ($r.Status -eq 204) $r.Status
$gone = Send 'GET' "$base/tasks/$del" $null $null
Check '删除后 404' ($gone.Status -eq 404) $gone.Status

# ============================================================
Write-Host "`n===== FR-12  OpenAPI 导出 =====" -ForegroundColor Cyan
$r = Send 'GET' 'http://localhost:18080/v3/api-docs' $null $null
Check '/v3/api-docs 可访问' ($r.Status -eq 200) $r.Status
Check '包含 tasks:batch' ($r.Body -like '*/api/v1/tasks:batch*') 'missing'
Check '包含 history 端点' ($r.Body -like '*/api/v1/tasks/{id}/history*') 'missing'

# ============================================================
Write-Host "`n===== 错误契约一致性 =====" -ForegroundColor Cyan
$r = Send 'GET' "$base/tasks/00000000-0000-0000-0000-000000000000" $null $null
Check '404 code=404' ($r.Status -eq 404 -and $r.Json.code -eq 404) "$($r.Status)/$($r.Json.code)"
$r = Send 'POST' "$base/tasks" @{ title = 'x'; estimateMin = 9999 } $null
Check '400 code=400' ($r.Status -eq 400 -and $r.Json.code -eq 400) "$($r.Status)/$($r.Json.code)"
Check '400 message 指出字段' ($r.Json.message -like '*estimateMin*') $r.Json.message
$r = Send 'POST' "$base/calendar/busy" @{} $null
Check '405 方法不允许' ($r.Status -eq 405 -and $r.Json.code -eq 405) "$($r.Status)/$($r.Json.code)"

# ============================================================
Write-Host "`n================================" -ForegroundColor Yellow
Write-Host "  PASS: $script:pass    FAIL: $script:fail" -ForegroundColor $(if ($script:fail -eq 0) { 'Green' } else { 'Red' })
Write-Host "================================" -ForegroundColor Yellow
$client.Dispose()
