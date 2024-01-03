sources = files(
    'main.rs',
)

app_name = meson.project_name()

if get_option('offline')
  offline = '--offline'
else
  offline = ''
endif


custom_target('speech-provider-espeak',
              build_by_default: true,
              input: [sources],
              output: app_name,
              console: true,
              install: true,
              install_dir: bindir,
              command: [cargo_script,
                  meson.project_build_root(),
                  meson.project_source_root(),
                  '@OUTPUT@',
                  app_name,
                  offline])
