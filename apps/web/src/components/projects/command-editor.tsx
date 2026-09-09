import type { components } from '@/lib/api/schema';
import { Field } from './form-fields';

type Command = components['schemas']['CommandSpec'];
const kinds: components['schemas']['StepKind'][] = ['bootstrap', 'install', 'migration', 'seed', 'test', 'lint', 'typecheck', 'build', 'custom_named'];
export function CommandEditor({ value, onChange, errors = {} }: {
  value: Command; onChange: (command: Command) => void; errors?: Record<string, string>;
}) {
  function argument(index: number, text: string) {
    onChange({ ...value, argv: value.argv.map((item, offset) => offset === index ? text : item) });
  }
  return <>
    <label>Command kind <select value={value.kind} onChange={event => onChange({ ...value, kind: event.target.value as Command['kind'] })}>
      {kinds.map(kind => <option key={kind}>{kind}</option>)}
    </select></label>
    <Field label="Command name" required value={value.name} onChange={name => onChange({ ...value, name })} error={errors.name} />
    <Field label="Executable" required value={value.argv[0] ?? ''} onChange={text => argument(0, text)} error={errors['argv.0'] ?? errors.argv} />
    {value.argv.slice(1).map((item, index) => <div key={index}>
      <Field label={`Argument ${index + 1}`} required value={item} onChange={text => argument(index + 1, text)} error={errors[`argv.${index + 1}`]} />
      <button type="button" onClick={() => onChange({ ...value, argv: value.argv.filter((_, offset) => offset !== index + 1) })}>Remove argument {index + 1}</button>
    </div>)}
    <button type="button" onClick={() => onChange({ ...value, argv: [...value.argv, ''] })}>Add argument</button>
    <Field label="Timeout seconds" type="number" min={1} max={7200} required value={value.timeout_seconds}
      onChange={text => onChange({ ...value, timeout_seconds: Number(text) })} error={errors.timeout_seconds} />
    <label><input type="checkbox" checked={value.required} onChange={event => onChange({ ...value, required: event.target.checked })} />Required command</label>
    <label><input type="checkbox" checked={value.network_enabled} onChange={event => onChange({ ...value, network_enabled: event.target.checked })} />Allow network for this command</label>
    <Field label="Allowed environment keys" multiline value={value.environment_keys.join('\n')}
      onChange={text => onChange({ ...value, environment_keys: text.split(/\r?\n/) })} error={errors.environment_keys} />
  </>;
}
