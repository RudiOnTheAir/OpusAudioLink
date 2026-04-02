import os, sys

plugins_file = '/build/.flutter-plugins'
plugins = {}
if os.path.exists(plugins_file):
    for line in open(plugins_file):
        line = line.strip()
        if line and not line.startswith('#'):
            name, path = line.split('=', 1)
            plugins[name.strip()] = path.strip()

includes = '\n'.join('include ":' + name + '"' for name in plugins)
plugin_projects = '\n'.join(
    'project(":' + name + '").projectDir = file("' + path + 'android")'
    for name, path in plugins.items()
    if os.path.isdir(path + 'android')
)

lines = [
    'pluginManagement {',
    '    def flutterSdkPath = {',
    '        def properties = new Properties()',
    '        file("local.properties").withInputStream { properties.load(it) }',
    '        def flutterSdkPath = properties.getProperty("flutter.sdk")',
    '        assert flutterSdkPath != null, "flutter.sdk not set in local.properties"',
    '        return flutterSdkPath',
    '    }()',
    '    includeBuild("$flutterSdkPath/packages/flutter_tools/gradle")',
    '    repositories { google(); mavenCentral(); gradlePluginPortal() }',
    '}',
    '',
    'plugins { id "dev.flutter.flutter-gradle-plugin" version "1.0.0" apply false }',
    '',
    'include ":app"',
]
if includes:
    lines.append(includes)
lines.append('')
if plugin_projects:
    lines.append(plugin_projects)

with open('/build/android/settings.gradle', 'w') as f:
    f.write('\n'.join(lines) + '\n')

print('settings.gradle mit ' + str(len(plugins)) + ' Plugins:')
for name in plugins:
    print('  - ' + name)
