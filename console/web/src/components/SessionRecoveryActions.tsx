import { useSyncExternalStore } from 'react';
import Button from '@cloudscape-design/components/button';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { authSession } from '../lib/authSession';

export function SessionRecoveryActions() {
  const auth = useSyncExternalStore(authSession.subscribe, authSession.getSnapshot);
  return <SpaceBetween direction="horizontal" size="xs">
    {auth.loginUrl && <Button href={auth.loginUrl} target="_blank" rel="noopener noreferrer">Sign in in a new tab</Button>}
    <Button loading={auth.checking} onClick={() => void authSession.check()}>Check sign-in</Button>
  </SpaceBetween>;
}
