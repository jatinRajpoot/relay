$ErrorActionPreference = 'Stop'
$projectId = 'aitryout-485605'
$region = 'asia-south1'
$baseUrl = 'https://relay-42733239266.asia-south1.run.app'
gcloud run deploy relay --source . --project $projectId --region $region `
    --allow-unauthenticated `
    --service-account "relay-run@$projectId.iam.gserviceaccount.com" `
    --execution-environment gen1 --cpu 0.25 --memory 256Mi --concurrency 1 `
    --min 0 --max 1 --min-instances 0 --max-instances 1 `
    --cpu-throttling --no-cpu-boost --timeout 300 `
    --set-env-vars "ENV_FILE=/secrets/relay/.env,BASE_URL=$baseUrl,DATABASE_PATH=/tmp/relay.sqlite3,DATABASE_BUCKET=$projectId-relay-data,MAX_UPLOAD_BYTES=20971520,YOUTUBE_REDIRECT_URI=$baseUrl/oauth/youtube/callback,INSTAGRAM_REDIRECT_URI=$baseUrl/oauth/instagram/callback" `
    --set-secrets '/secrets/relay/.env=relay-config:1' --quiet
if ($LASTEXITCODE -ne 0) { throw 'Cloud Run deployment failed.' }
