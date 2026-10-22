import * as React from 'react';
import { captureException, withScope } from 'insight-sdk';

/**
 * <ErrorBoundary fallback={<p>Something went wrong</p>}> … </ErrorBoundary>
 * Reports render errors (with the component stack) and shows `fallback`.
 */
export class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(error) {
    return { error };
  }
  componentDidCatch(error, info) {
    withScope((scope) => {
      scope.setTag('mechanism', 'react.ErrorBoundary');
      scope.setExtra('componentStack', info && info.componentStack);
      captureException(error);
    });
    if (this.props.onError) this.props.onError(error, info);
  }
  reset = () => this.setState({ error: null });
  render() {
    if (this.state.error === null) return this.props.children;
    const f = this.props.fallback;
    return typeof f === 'function' ? f({ error: this.state.error, reset: this.reset }) : f === undefined ? null : f;
  }
}
