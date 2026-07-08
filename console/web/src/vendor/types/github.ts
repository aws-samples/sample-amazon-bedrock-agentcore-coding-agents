/**
 * GitHub App ("the code app") types: installation, repo, webhook envelope.
 */

export interface GitHubInstallation {
  installation_id: number;
  account_login: string;          // org or user login
  account_type: 'Organization' | 'User';
  scopes: string[];
  repos_selected: 'all' | number[]; // 'all' or specific repo IDs
  installed_at: string;             // ISO 8601
  user_id: string;                  // the relay user owning the install
}

export interface Repo {
  installation_id: number;
  full_name: string;                // "org/repo"
  default_branch: string;
  language?: string;
  last_synced_at?: string;
}

export type WebhookEvent =
  | 'issues'
  | 'issue_comment'
  | 'pull_request'
  | 'pull_request_review_comment'
  | 'check_suite'
  | 'check_run'
  | 'installation'
  | 'installation_repositories';

export interface WebhookEnvelope<T = unknown> {
  event: WebhookEvent;
  delivery_id: string;
  installation_id: number;
  payload: T;
  signature_valid: boolean;
}
