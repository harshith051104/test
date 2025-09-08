from fastapi import FastAPI, Request, HTTPException
import hmac
import hashlib
import uvicorn
import json
 
app = FastAPI()
 
# Replace with your Jira webhook secret token
WEBHOOK_SECRET = 'E6F7NIhtiRRS0AtLk2aW'  # Generated in Jira webhook config
 
def is_valid_signature(secret: str, payload: bytes, header_signature: str) -> bool:
    """Validate HMAC SHA256 signature."""
    if not header_signature:
        return False
    method, signature = header_signature.split('=')
    hash_object = hmac.new(
        secret.encode('utf-8'),
        msg=payload,
        digestmod=hashlib.sha256
    )k sipokookok
    calculated_signature = method + '=' + hash_object.hexdigest()
    return hmac.compare_digest(calculated_signature, header_signature)
 
@app.get("/")
def Server():
    return "..."


@app.post('/webhook')
async def handle_jira_webhook(request: Request):
    # Validate signature
    header_signature = request.headers.get('X-Hub-Signature')
    payload = await request.body()  # Get raw body for validation
    if not is_valid_signature(WEBHOOK_SECRET, payload, header_signature):
        raise HTTPException(status_code=403, detail='Invalid or missing signature')
   
    # Parse JSON payload
    data = await request.json()
    event = data.get('webhookEvent')
    issue = data.get('issue')
   
    print(f"📥 Received webhook event: {event}")
    if issue:
        project_key = issue['fields']['project']['key']
        status_name = issue['fields']['status']['name']
        print(f"📋 Issue: {issue.get('key', 'N/A')}, Project: {project_key}, Status: {status_name}")
   
    # Optional: Additional runtime check (simulates JQL; actual filter is in Jira)
    # Temporarily disabled for testing
    # if issue:
    #     project_key = issue['fields']['project']['key']
    #     status_name = issue['fields']['status']['name']
    #     if project_key != 'TEST' or status_name != 'Done':  # Example filter
    #         print(f"⚠️  Event filtered out (Project: {project_key}, Status: {status_name})")
    #         return {'message': 'Ignored due to filter'}
   
    # Save the full payload to a local JSON file (appends each payload as a new line)
    try:
        with open('webhook_data/webhook_payloads.json', 'a') as f:
            f.write(json.dumps(data) + '\n')
        print(f"✅ Payload saved to webhook_data/webhook_payloads.json")
    except Exception as e:
        print(f"❌ Error saving payload: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error saving payload: {str(e)}")
   
    # Process the event (e.g., log or integrate with other systems)
    print(f"Event: {event}")
    if issue:
        print(f"Issue Key: {issue['key']}, Summary: {issue['fields']['summary']}")
   
    return {'message': 'Webhook processed and saved to file'}
 
if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=8000, reload=True)