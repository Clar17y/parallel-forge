import Link from 'next/link';
import type { components } from '@/lib/api/schema';

export function ProjectList({ projects }: { projects: components['schemas']['ProjectResponse'][] }) {
  if (!projects.length) return <p>No projects registered yet.</p>;
  return <div className="table-scroll"><table><thead><tr><th>Project</th><th>Repository</th><th>Base</th><th>Policy</th></tr></thead>
    <tbody>{projects.map(project => <tr key={project.id}>
      <td><Link href={`/projects/${project.id}`}>{project.name}</Link></td>
      <td>{project.github_repository}</td><td>{project.default_branch}</td><td>{project.policy_version ?? 'Unavailable'}</td>
    </tr>)}</tbody></table></div>;
}
