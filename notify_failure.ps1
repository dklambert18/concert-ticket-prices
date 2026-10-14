# Shows a Windows notification saying the collector run failed.
# Called by run_collector.bat when collector.py exits with an error.
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]

$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@"
<toast scenario="reminder">
  <visual>
    <binding template="ToastGeneric">
      <text>Concert ticket collector failed</text>
      <text>Today's run hit an error. Check collector.log for details.</text>
    </binding>
  </visual>
  <actions>
    <action content="Dismiss" arguments="dismiss" activationType="system"/>
  </actions>
</toast>
"@)

# Toasts must come from a registered app; use Windows PowerShell's app ID.
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show(
    [Windows.UI.Notifications.ToastNotification]::new($xml))
