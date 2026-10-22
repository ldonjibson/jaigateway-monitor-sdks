import 'package:flutter/material.dart';
import 'package:insight_flutter/insight_flutter.dart';

Future<void> main() async {
  await Insight.run(
    InsightOptions(
      apiKey: 'YOUR_WRITE_KEY',
      host: 'https://insight.example.com',
      release: 'example@1.0.0',
      environment: 'development',
      appPackage: 'insight_example',
      debug: true,
    ),
    () => runApp(const ExampleApp()),
  );
}

class ExampleApp extends StatelessWidget {
  const ExampleApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      navigatorObservers: [InsightNavigatorObserver()],
      home: Scaffold(
        appBar: AppBar(title: const Text('Insight example')),
        body: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              ElevatedButton(
                onPressed: () => throw StateError('Example crash'),
                child: const Text('Throw an error'),
              ),
              ElevatedButton(
                onPressed: () => Insight.captureMessage('Hello from the example'),
                child: const Text('Send a message'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
