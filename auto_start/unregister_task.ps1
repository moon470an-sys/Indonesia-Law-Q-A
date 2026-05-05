$TaskName = "Indonesia Law RAG"
try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Output "✅ '$TaskName' 등록 해제 완료"
} catch {
    Write-Output "Task가 등록되어 있지 않거나 제거할 수 없습니다: $_"
}
