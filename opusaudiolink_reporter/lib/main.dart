import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'main_page.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  // Hochformat erzwingen
  SystemChrome.setPreferredOrientations([
    DeviceOrientation.portraitUp,
  ]);
  // Vollbild / immersive
  SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
  runApp(const OpusAudioLinkApp());
}

class OpusAudioLinkApp extends StatelessWidget {
  const OpusAudioLinkApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'OpusAudioLink',
      debugShowCheckedModeBanner: false,
      theme: ThemeData.dark().copyWith(
        scaffoldBackgroundColor: const Color(0xFF1E1E1E),
        colorScheme: const ColorScheme.dark(
          primary:   Color(0xFF4A7ACC),
          secondary: Color(0xFF2A8C3A),
          surface:   Color(0xFF2D2D2D),
        ),
        appBarTheme: const AppBarTheme(
          backgroundColor: Color(0xFF1E1E1E),
          foregroundColor: Color(0xFFE0E0E0),
          elevation: 0,
        ),
        elevatedButtonTheme: ElevatedButtonThemeData(
          style: ElevatedButton.styleFrom(
            backgroundColor: const Color(0xFF3A3A3A),
            foregroundColor: const Color(0xFFE0E0E0),
          ),
        ),
      ),
      home: const MainPage(),
    );
  }
}
