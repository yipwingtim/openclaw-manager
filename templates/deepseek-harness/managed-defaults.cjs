// Run as the Harness user, including when preparing an existing instance.
const { mkdir, readFile, readlink, symlink } = require('node:fs/promises');
const { join } = require('node:path');
const { createRequire } = require('node:module');
const fromHarness = createRequire('/usr/local/lib/node_modules/@deepseek-ai/dsh/package.json');
const fromCredentials = createRequire(fromHarness.resolve('@deepseek-ai/dsh-credentials-local/package.json'));
const { parseDocument, isMap, isSeq } = fromCredentials('yaml');
const { withFileLock, writeFileAtomic } = fromHarness('@deepseek-ai/dsh-atomic-write');

async function documentAt(path, empty) {
  let source;
  try { source = await readFile(path, 'utf8'); }
  catch (error) { if (error.code !== 'ENOENT') throw error; source = empty; }
  const document = parseDocument(source, { customTags: [{
    tag: 'tag:yaml.org,2002:js', resolve: value => value,
  }] });
  if (document.errors.length) throw Error('Invalid Harness YAML');
  return document;
}

async function prepare() {
  const home = process.env.DSH_HOME;
  const dir = join(home, 'profiles/web');
  const documents = join(home, 'manager-documents');
  const parent = join(documents, 'deepseek-harness');
  await mkdir(parent, { recursive: true });
  const workspace = join(parent, 'default-workspace');
  try { await symlink('/workspace', workspace); }
  catch (error) {
    if (error.code !== 'EEXIST' || await readlink(workspace) !== '/workspace') throw error;
  }
  await mkdir(dir, { recursive: true });
  const creds = join(home, '.credentials.yaml');
  const ref = 'OPENCLAW_MANAGER_MODEL_PROXY_TOKEN';
  await withFileLock(creds, async () => {
    const doc = await documentAt(creds, 'version: 1\n');
    if (!doc.contents) doc.contents = doc.createNode({version: 1});
    if (doc.get('version') !== 1) throw Error('Unsupported Harness credential format');
    if (doc.getIn(['refs', ref]) === process.env[ref]) return;
    doc.setIn(['refs', ref], process.env[ref]);
    await writeFileAtomic(creds, String(doc), {mode: 0o600});
  });
  await withFileLock(join(dir, 'package.json'), async () => {
    const file = join(dir, 'cordis.patch.yml');
    const doc = await documentAt(file, '[]');
    if (!isSeq(doc.contents)) throw Error('Expected a Harness patch list');
    const original = String(doc);
    function config(id) {
      let entry = doc.contents.items.findLast(item => isMap(item) && item.get('id') === id);
      if (!entry) { entry = doc.createNode({id}); doc.contents.add(entry); }
      if (!entry.has('config')) entry.set('config', doc.createNode({}));
      const value = entry.get('config', true);
      if (!isMap(value)) throw Error('Unsupported Harness plugin configuration');
      return value;
    }
    const provider = 'openclaw-manager';
    const model = process.env.HARNESS_MODEL_ID;
    const llm = config('llm-pi-ai');
    if (!llm.hasIn(['providers', provider])) {
      llm.setIn(['providers', provider], doc.createNode({
        displayName: 'Model Proxy', api: 'openai-completions', apiKeyEnv: ref,
        baseURL: process.env.HARNESS_MODEL_PROXY_URL, models: [{id: model}],
      }));
    }
    const selection = config('agent-default-model');
    if (!selection.has('provider') && !selection.has('model')) {
      selection.set('provider', provider);
      selection.set('model', model);
    }
    const picker = config('workspace-controller');
    if (!picker.has('documentsDirectory')) picker.set('documentsDirectory', documents);
    config('ui-settings-models').set('credentialOnboarding', false);
    if (String(doc) !== original) await writeFileAtomic(file, String(doc), {mode: 0o600});
  });
}

prepare().catch(() => { console.error('Could not prepare managed Harness profile or credentials'); process.exitCode = 1; });
