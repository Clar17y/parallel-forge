import Link from 'next/link';
import type { components } from '@/lib/api/schema';

export function ProjectList({ projects }: { projects: components['schemas']['ProjectResponse'][] }) {
  if (!projects.length) return <><p>No projects registered yet.</p><p>A project connects Forge to one local repository: it pins the commands agents may run and the models they use. <Link href="/projects/new">Register your first project</Link></p></>;
  return <div className="table-scroll"><table className="data-table"><thead><tr><th>Project</th><th>Repository</th><th>Base branch</th><th>Policy version</th></tr></thead>
    <tbody>{projects.map(project => <tr key={project.id}>
      <td className="font-semibold"><Link href={`/projects/${project.id}`}>{project.name}</Link></td>
      <td><code>{project.github_repository}</code></td><td><code>{project.default_branch}</code></td><td>{project.policy_version != null ? `v${project.policy_version}` : 'Unavailable'}</td>
    </tr>)}</tbody></table></div>;
}
