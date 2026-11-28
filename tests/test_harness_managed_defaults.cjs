// Run inside the pinned Harness image (see the adapter guide).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { createRequire } = require('node:module');
const harness = createRequire('/usr/local/lib/node_modules/@deepseek-ai/dsh/package.json');
const yaml = createRequire(harness.resolve('@deepseek-ai/dsh-credentials-local/package.json'))('yaml');

test('first use, personal settings, and restored proxy credentials', () => {
  const home = fs.mkdtempSync('/tmp/harness-profile-');
  const env = {...process.env, DSH_HOME: home, HARNESS_MODEL_ID: 'test-model',
    HARNESS_MODEL_PROXY_URL: 'http://proxy:8081/v1', OPENCLAW_MANAGER_MODEL_PROXY_TOKEN: 'test-token'};
  const prepare = () => execFileSync(process.execPath, [path.join(__dirname, '../templates/deepseek-harness/managed-defaults.cjs')], {env});
  const profile = path.join(home, 'profiles/web/cordis.patch.yml');
  const credentials = path.join(home, '.credentials.yaml');
  try {
    prepare();
    const entries = yaml.parse(fs.readFileSync(profile, 'utf8'));
    const config = id => entries.find(entry => entry.id === id).config;
    assert.equal(config('agent-default-model').model, 'test-model');
    assert.equal(config('ui-settings-models').credentialOnboarding, false);
    assert.equal(fs.readlinkSync(path.join(config('workspace-controller').documentsDirectory, 'deepseek-harness/default-workspace')), '/workspace');
    config('llm-pi-ai').providers.personal = {apiKeyEnv: 'PERSONAL'};
    config('agent-default-model').provider = 'personal';
    config('agent-default-model').model = 'personal-model';
    fs.writeFileSync(profile, yaml.stringify(entries));
    const personal = fs.readFileSync(profile, 'utf8');
    const creds = yaml.parse(fs.readFileSync(credentials, 'utf8'));
    creds.refs.PERSONAL = 'personal-test-key';
    fs.writeFileSync(credentials, yaml.stringify(creds));
    env.OPENCLAW_MANAGER_MODEL_PROXY_TOKEN = 'restored-token';
    prepare();
    assert.equal(fs.readFileSync(profile, 'utf8'), personal);
    assert.deepEqual(yaml.parse(fs.readFileSync(credentials, 'utf8')).refs,
      {OPENCLAW_MANAGER_MODEL_PROXY_TOKEN: 'restored-token', PERSONAL: 'personal-test-key'});
  } finally { fs.rmSync(home, {recursive:true, force:true}); }
});
