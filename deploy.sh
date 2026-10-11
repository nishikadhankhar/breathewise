#!/usr/bin/env bash
# Deploy BreatheWise to AWS.   Usage: ./deploy.sh you@example.com
#
# Needs the AWS CLI, signed in (run `aws configure` once). Safe to run again:
# it updates the same stack. Remove everything with ./deploy.sh --delete
set -euo pipefail
cd "$(dirname "$0")"

STACK="${STACK:-breathewise}"
REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$REGION"

command -v aws >/dev/null || { echo "The AWS CLI is not installed. See README.md, step 2."; exit 1; }
ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
  || { echo "The AWS CLI is not signed in. Run: aws configure"; exit 1; }

output() { aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

if [[ "${1:-}" == "--delete" ]]; then
  BUCKET="$(output SchoolsBucketName || true)"
  [[ -n "$BUCKET" && "$BUCKET" != "None" ]] && aws s3 rm "s3://$BUCKET" --recursive
  aws cloudformation delete-stack --stack-name "$STACK"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK"
  echo "Deleted the $STACK stack. The code bucket breathewise-code-$ACCOUNT-$REGION is kept; delete it by hand if you want."
  exit 0
fi

EMAIL="${1:-}"
[[ "$EMAIL" == *@*.* ]] || { echo "Usage: ./deploy.sh you@example.com   (the address that gets the evening plan)"; exit 1; }

echo "Running tests..."
python3 -m unittest discover -s tests >/dev/null 2>&1 || { echo "Tests failed. Run: python3 -m unittest discover -s tests"; exit 1; }

echo "Packaging the code..."
rm -rf build .packaged.yaml
mkdir -p build
cp -R app web build/
find build -name '__pycache__' -type d -prune -exec rm -rf {} +

CODE_BUCKET="breathewise-code-$ACCOUNT-$REGION"
aws s3api head-bucket --bucket "$CODE_BUCKET" 2>/dev/null || aws s3 mb "s3://$CODE_BUCKET" >/dev/null
aws cloudformation package --template-file template.yaml --s3-bucket "$CODE_BUCKET" --output-template-file .packaged.yaml >/dev/null

echo "Deploying to account $ACCOUNT in $REGION (first run takes a few minutes)..."
aws cloudformation deploy --template-file .packaged.yaml --stack-name "$STACK" \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --parameter-overrides "AlertEmail=$EMAIL" \
    "UseBedrock=${USE_BEDROCK:-0}" \
    ${ADMIN_PASSCODE:+"AdminPasscode=$ADMIN_PASSCODE"} \
    ${DOMAIN_NAME:+"DomainName=$DOMAIN_NAME"} ${CERTIFICATE_ARN:+"CertificateArn=$CERTIFICATE_ARN"} \
    "BedrockModelId=${BEDROCK_MODEL_ID:-eu.amazon.nova-lite-v1:0}" "BedrockRegion=${BEDROCK_REGION:-eu-north-1}" \
  --no-fail-on-empty-changeset

# Settings changed from the web page live in the bucket, so a school already there is left
# alone unless you ask for it to be replaced with RESET_SCHOOLS=1.
echo "Uploading the schools..."
BUCKET="$(output SchoolsBucketName)"
for dir in data/schools/*/; do
  id="$(basename "$dir")"
  if [[ "${RESET_SCHOOLS:-0}" == "1" ]] || ! aws s3api head-object --bucket "$BUCKET" --key "schools/$id/school.json" >/dev/null 2>&1; then
    aws s3 sync "$dir" "s3://$BUCKET/schools/$id" --exclude '.DS_Store' >/dev/null
    echo "  $id: uploaded"
  else
    echo "  $id: kept as it is on the site (RESET_SCHOOLS=1 replaces it)"
  fi
done

echo
echo "Done. Open: $(output WebUrl)"
echo "Check your inbox ($EMAIL) and confirm the AWS subscription, or the evening plan will not arrive."
echo "Send the evening plan now instead of waiting:  aws lambda invoke --function-name $(output FunctionName) /dev/stdout"
