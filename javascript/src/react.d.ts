import * as React from 'react';
export interface ErrorBoundaryProps {
  children?: React.ReactNode;
  fallback?: React.ReactNode | ((p: { error: unknown; reset: () => void }) => React.ReactNode);
  onError?: (error: unknown, info: React.ErrorInfo) => void;
}
export class ErrorBoundary extends React.Component<ErrorBoundaryProps, { error: unknown }> {
  reset: () => void;
}
