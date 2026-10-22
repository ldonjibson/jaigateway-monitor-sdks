/// Insight SDK for Flutter: error capture, breadcrumbs, batched transport.
library insight_flutter;

export 'src/breadcrumbs.dart' show Breadcrumb, BreadcrumbBuffer;
export 'src/event.dart' show InsightLevel, InsightUser;
export 'src/http_client.dart' show InsightHttpClient, sanitizeUrl;
export 'src/insight.dart' show Insight, InsightClient;
export 'src/navigator_observer.dart' show InsightNavigatorObserver;
export 'src/options.dart' show InsightOptions, BeforeSendCallback, normalizeIngestUrl, insightSdkVersion;
export 'src/stacktrace.dart' show parseStackTrace, isInAppFrame;
export 'src/transport.dart' show InsightTransport;
