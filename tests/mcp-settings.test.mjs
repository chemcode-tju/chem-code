import assert from 'node:assert/strict';
import test from 'node:test';
import {mcpConfiguration} from '../src/chemistry_agent/web/mcp-settings.mjs';

test('HTTP uses the browser tunnel while stdio uses the actual platform host port', () => {
  const connection={http_path:'/api/knowledge/mcp',stdio_command:'/opt/chem/bin/python',
    stdio_args:['-m','chemistry_agent.knowledge.mcp'],stdio_env:{PYTHONPATH:'/opt/chem/src'},
    platform_origin:'http://127.0.0.1:8766'};
  const generated=mcpConfiguration('http://localhost:9000','project&1',connection);
  assert.equal(new URL(generated.url).port,'9000');
  assert.equal(new URL(generated.url).searchParams.get('project_id'),'project&1');
  const env=generated.config.mcpServers['chem-knowledge'].env;
  assert.equal(new URL(env.CHEM_KNOWLEDGE_URL).port,'8766');
  assert.equal(new URL(env.CHEM_KNOWLEDGE_URL).searchParams.get('project_id'),'project&1');
  assert.equal(env.PYTHONPATH,'/opt/chem/src');
  assert.equal(new URL(mcpConfiguration('http://localhost:9000','',connection).url).search,'');
});
