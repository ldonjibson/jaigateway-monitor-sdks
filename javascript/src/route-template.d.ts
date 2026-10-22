export function templateFromParams(pathname: string, params: Record<string, string | string[] | undefined>): string;
export function fromColonPath(path: string): string;
export function fromReactRouterMatches(matches: { route?: { path?: string } }[]): string | null;
export function fromNextBracketPath(pathname: string): string;
