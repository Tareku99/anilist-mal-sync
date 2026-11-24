# AniList-MAL Sync troubleshooting

## OAuth Authentication Fails

- Verify your OAuth redirect URIs match exactly (no trailing slashes, correct protocol)
- Check that your client ID and secret are correct
- Ensure the OAuth callback port (default: 18080) is accessible

## Sync Not Working

- Check that tokens are valid: `anilist-mal-sync auth`
- Verify your configuration is valid
- Check logs for error messages
- Try running with `--dry-run` to see what would be synced

## Configuration Issues

- Ensure all required fields are filled in `data/config.yaml`
- Check for placeholder values that need to be replaced
- Verify YAML syntax is correct (no tabs, proper indentation)
