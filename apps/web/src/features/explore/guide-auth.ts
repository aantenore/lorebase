import { useCredentials, useSession } from '@/api/provider';
import { useConfig } from '@/config/context';
import type { GuideAuth } from '@/api/guide-client';
import type { ExploreTransport } from './explore-client';
import { liveTransport } from './explore-client';
import { mockTransport } from '@/api/mock/explore';

/**
 * The caller's credential for Guide, read when a request is made: in session mode the session
 * cookie (Guide behind the app's origin) and the CSRF header, otherwise the Bearer token Keeper
 * gets. The app holds no credential of Guide's own.
 */
export function useGuideAuth(): () => GuideAuth {
  const { authMode, apiMode } = useConfig();
  const credentials = useCredentials();
  const session = useSession();
  return (): GuideAuth => {
    if (apiMode === 'live' && authMode === 'session' && session !== null) {
      return { headers: session.csrfHeaders('POST'), credentials: 'include' };
    }
    const token = credentials.token();
    return {
      headers: token === null ? {} : { Authorization: `Bearer ${token}` },
      credentials: 'same-origin',
    };
  };
}

/**
 * Where explorations come from: the scripted guide in mock mode, `{guideUrl}/explore` in live
 * mode, and nothing (`null`) in live mode without a guide service.
 */
export function useExploreTransport(): ExploreTransport | null {
  const { apiMode, guideUrl, explore } = useConfig();
  const auth = useGuideAuth();
  if (apiMode === 'mock') return mockTransport(explore.mockStepMs);
  return guideUrl === null ? null : liveTransport(guideUrl, auth);
}
