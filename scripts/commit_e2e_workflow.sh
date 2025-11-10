#!/bin/bash
# Quick commit and push for E2E workflow testing

echo "🚀 Committing E2E GitHub Actions workflow..."

git add .github/workflows/e2e-mcp-gateway.yml
git add scripts/gateway_e2e_run.sh
git add docs/E2E_GITHUB_ACTIONS_GUIDE.md

git status

echo ""
echo "Ready to commit. Press Enter to continue or Ctrl+C to cancel..."
read

git commit -m "Add E2E GitHub Actions workflow with MCP Gateway

- Build docker-mcp CLI plugin from source in CI
- Run complete E2E validation via gateway_e2e_run.sh
- Fix bash script parity with PowerShell (secret name, env vars)
- Add CI-aware cleanup (auto-delete validation profile)
- Upload test report artifacts
- Display pass/fail summary in GitHub Actions UI
- Support workflow_dispatch with allow_live_writes toggle"

echo ""
echo "✅ Committed! Now push with:"
echo "   git push origin $(git branch --show-current)"
