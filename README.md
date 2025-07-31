# Flow Log Forwarder (Azure Functions - Python 3.11)

Ingests Azure Network Watcher flow logs from a Storage Account (Blob trigger), parses them, and forwards normalized records to Azure Event Hubs using **managed identity**.

## Configure App Settings
In your Function App:

- `FlowLogsStorage__blobServiceUri = https://<FLOWLOG_ACCOUNT>.blob.core.windows.net`
- `FlowLogsStorage__queueServiceUri = https://<FLOWLOG_ACCOUNT>.queue.core.windows.net`
- `FlowLogsStorage__credential = managedidentity`
- `EVENTHUB_FQDN = <namespace>.servicebus.windows.net`
- `EVENTHUB_NAME = <event-hub-name>`

## Roles to assign to the Function's system-assigned identity
- Storage account (that holds flow logs):
  - **Storage Blob Data Reader**
  - **Storage Queue Data Contributor**
- Event Hubs (namespace or hub scope):
  - **Azure Event Hubs Data Sender**

## Local development
Install Azure Functions Core Tools v4 and Python 3.11.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
func start
```

## Deploy (easiest)
```bash
func azure functionapp publish <YOUR_FUNCTION_APP_NAME> --python
```

## Zip deploy (if not using Core Tools)
Create a zip of this folder and upload via **Deployment Center** or:

```bash
zip -r flowlog-forwarder.zip .
az functionapp deployment source config-zip -g <RG> -n <APP> --src flowlog-forwarder.zip
# Ensure the following app setting exists so dependencies build on the server:
az functionapp config appsettings set -g <RG> -n <APP> --settings SCM_DO_BUILD_DURING_DEPLOYMENT=true
```
