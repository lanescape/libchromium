// A minimal Content API embedder, built outside Chromium's tree against a packaged SDK (tests/embedder/build.py),
// that checks the SDK works end to end: it starts Content, loads a page whose script computes a value, and the
// default margin Blink's user-agent style sheet, one of the SDK's resources, gives the page, reads that back
// through JavaScript in an isolated world, asks the frame's renderer for the page's title over an interface of its
// own (probe.mojom), and exits 0 if both are right (specs/0003-ci-cd.md, CI).
//
// It implements Content's delegate interfaces in its own binary, so the library calls back across its boundary,
// and it runs Chromium's renderer and GPU processes from its own executable, sandboxed as Chromium sets up. Its
// renderer side answers the browser over that interface, whose bindings build.py generates with the SDK's mojom
// tooling.

#include <stdio.h>

#include <memory>
#include <optional>
#include <string>
#include <utility>

#include "base/base_paths.h"
#include "base/command_line.h"
#include "base/files/file_path.h"
#include "base/files/scoped_temp_dir.h"
#include "base/functional/bind.h"
#include "base/functional/callback.h"
#include "base/location.h"
#include "base/memory/ref_counted_memory.h"
#include "base/memory/scoped_refptr.h"
#include "base/path_service.h"
#include "base/run_loop.h"
#include "base/strings/utf_string_conversions.h"
#include "base/task/single_thread_task_runner.h"
#include "base/time/time.h"
#include "base/values.h"
#include "build/build_config.h"
#include "components/embedder_support/user_agent_utils.h"
#include "content/public/app/content_main.h"
#include "content/public/app/content_main_delegate.h"
#include "content/public/browser/browser_context.h"
#include "content/public/browser/browser_main_parts.h"
#include "content/public/browser/content_browser_client.h"
#include "content/public/browser/navigation_controller.h"
#include "content/public/browser/render_frame_host.h"
#include "content/public/browser/web_contents.h"
#include "content/public/browser/web_contents_observer.h"
#include "content/public/common/content_client.h"
#include "content/public/common/isolated_world_ids.h"
#include "content/public/renderer/content_renderer_client.h"
#include "content/public/renderer/render_frame.h"
#include "content/public/renderer/render_frame_observer.h"
#include "mojo/public/cpp/bindings/associated_receiver.h"
#include "mojo/public/cpp/bindings/associated_remote.h"
#include "mojo/public/cpp/bindings/pending_associated_receiver.h"
#include "tests/embedder/platform.h"
#include "tests/embedder/probe.mojom.h"
#include "third_party/blink/public/common/associated_interfaces/associated_interface_provider.h"
#include "third_party/blink/public/common/input/web_mouse_event.h"
#include "third_party/blink/public/common/associated_interfaces/associated_interface_registry.h"
#include "third_party/blink/public/platform/web_string.h"
#include "third_party/blink/public/web/web_document.h"
#include "third_party/blink/public/web/web_local_frame.h"
#include "ui/base/resource/resource_bundle.h"
#include "ui/base/l10n/l10n_util.h"
#include "ui/base/resource/resource_scale_factor.h"
#include "ui/events/blink/web_input_event.h"
#include "ui/events/event.h"
#include "ui/events/event_constants.h"
#include "ui/gfx/geometry/rect.h"
#include "ui/gfx/image/image.h"
#include "url/gurl.h"

namespace embedder {
namespace {

// The page computes a value, Blink's user-agent style sheet gives the body its margin, and the GPU process, on
// SwiftShader, gives the page a WebGL context; the embedder only reads the result.
constexpr char kPage[] =
    "data:text/html,<title>pending</title><body><script>"
    "const gl = document.createElement('canvas').getContext('webgl');"
    "document.title = [6 * 7, getComputedStyle(document.body).marginTop,"
    " gl ? gl.getParameter(gl.VERSION).split(' ')[0] : 'no WebGL'].join(',');"
    "</script></body>";
constexpr char16_t kRead[] = u"document.title";
constexpr char kExpected[] = "42,8px,WebGL";
// Sent to the renderer and back, through the SDK's type mapping of gfx.mojom.Rect to gfx::Rect.
constexpr gfx::Rect kRect(3, 5, 7, 11);
constexpr base::TimeDelta kTimeout = base::Seconds(90);

int g_result = 1;

// Content and Blink ask for their resources and strings through the embedder's ContentClient, which serves them
// from the SDK's packs.
class ContentClient : public content::ContentClient {
 public:
  std::u16string GetLocalizedString(int message_id) override { return l10n_util::GetStringUTF16(message_id); }
  std::string_view GetDataResource(int resource_id, ui::ResourceScaleFactor scale_factor) override {
    return ui::ResourceBundle::GetSharedInstance().GetRawDataResourceForScale(resource_id, scale_factor);
  }
  scoped_refptr<base::RefCountedMemory> GetDataResourceBytes(int resource_id) override {
    return ui::ResourceBundle::GetSharedInstance().LoadDataResourceBytes(resource_id);
  }
  std::string GetDataResourceString(int resource_id) override {
    return ui::ResourceBundle::GetSharedInstance().LoadDataResourceString(resource_id);
  }
  gfx::Image& GetNativeImageNamed(int resource_id) override {
    return ui::ResourceBundle::GetSharedInstance().GetNativeImageNamed(resource_id);
  }
};

class BrowserContext : public content::BrowserContext {
 public:
  BrowserContext() { CHECK(directory_.CreateUniqueTempDir()); }
  ~BrowserContext() override {
    NotifyWillBeDestroyed();
    ShutdownStoragePartitions();
  }

  std::unique_ptr<content::ZoomLevelDelegate> CreateZoomLevelDelegate(const base::FilePath&) override {
    return nullptr;
  }
  base::FilePath GetPath() const override { return directory_.GetPath(); }
  bool IsOffTheRecord() override { return true; }
  content::DownloadManagerDelegate* GetDownloadManagerDelegate() override { return nullptr; }
  content::BrowserPluginGuestManager* GetGuestManager() override { return nullptr; }
  storage::SpecialStoragePolicy* GetSpecialStoragePolicy() override { return nullptr; }
  content::PlatformNotificationService* GetPlatformNotificationService() override { return nullptr; }
  content::PushMessagingService* GetPushMessagingService() override { return nullptr; }
  content::StorageNotificationService* GetStorageNotificationService() override { return nullptr; }
  content::SSLHostStateDelegate* GetSSLHostStateDelegate() override { return nullptr; }
  content::PermissionControllerDelegate* GetPermissionControllerDelegate() override { return nullptr; }
  content::ReduceAcceptLanguageControllerDelegate* GetReduceAcceptLanguageControllerDelegate() override {
    return nullptr;
  }
  content::ClientHintsControllerDelegate* GetClientHintsControllerDelegate() override { return nullptr; }
  content::BackgroundFetchDelegate* GetBackgroundFetchDelegate() override { return nullptr; }
  content::BackgroundSyncController* GetBackgroundSyncController() override { return nullptr; }
  content::BrowsingDataRemoverDelegate* GetBrowsingDataRemoverDelegate() override { return nullptr; }

 private:
  base::ScopedTempDir directory_;
};

// The renderer's side of probe.mojom, one for each frame: it answers with what Blink has as the document's title.
class ProbeAgent : public content::RenderFrameObserver, public mojom::Probe {
 public:
  explicit ProbeAgent(content::RenderFrame* frame) : content::RenderFrameObserver(frame) {
    frame->GetAssociatedInterfaceRegistry()->AddInterface<mojom::Probe>(
        base::BindRepeating(&ProbeAgent::Bind, base::Unretained(this)));
  }

  void Read(const gfx::Rect& rect, ReadCallback callback) override {
    std::move(callback).Run(render_frame()->GetWebFrame()->GetDocument().Title().Utf16(), rect);
  }

 private:
  void Bind(mojo::PendingAssociatedReceiver<mojom::Probe> receiver) {
    receiver_.reset();
    receiver_.Bind(std::move(receiver));
  }

  void OnDestruct() override { delete this; }

  mojo::AssociatedReceiver<mojom::Probe> receiver_{this};
};

class RendererClient : public content::ContentRendererClient {
 public:
  void RenderFrameCreated(content::RenderFrame* frame) override { new ProbeAgent(frame); }
};

// Waits for the page to load, then reads its result through an isolated world, as an embedder's own scripts do,
// and asks the frame's renderer for the same through probe.mojom.
class Check : public content::WebContentsObserver {
 public:
  Check(content::WebContents* contents, base::OnceClosure done)
      : content::WebContentsObserver(contents), done_(std::move(done)) {}

  void DidFinishLoad(content::RenderFrameHost* frame, const GURL& url) override {
    if (frame != web_contents()->GetPrimaryMainFrame() || asked_) {
      return;
    }
    asked_ = true;
    fprintf(stderr, "embedder: loaded %s\n", url.possibly_invalid_spec().substr(0, 40).c_str());
    frame->ExecuteJavaScriptInIsolatedWorld(kRead, base::BindOnce(&Check::OnResult, base::Unretained(this)),
                                            content::ISOLATED_WORLD_ID_CONTENT_END);
  }

  void PrimaryMainFrameRenderProcessGone(base::TerminationStatus status) override {
    fprintf(stderr, "embedder: the renderer is gone (status %d)\n", static_cast<int>(status));
    Finish(1);
  }

 private:
  void OnResult(base::Value value) {
    if (!value.is_string() || value.GetString() != kExpected) {
      fprintf(stderr, "embedder: expected \"%s\", got %s\n", kExpected, value.DebugString().c_str());
      Finish(1);
      return;
    }
    fprintf(stderr, "embedder: the page computed %s\n", kExpected);
    content::RenderFrameHost* frame = web_contents()->GetPrimaryMainFrame();
    frame->GetRemoteAssociatedInterfaces()->GetInterface(&probe_);
    probe_.set_disconnect_handler(base::BindOnce(&Check::OnProbeGone, base::Unretained(this)));
    probe_->Read(kRect, base::BindOnce(&Check::OnProbe, base::Unretained(this)));
  }

  void OnProbe(const std::u16string& title, const gfx::Rect& rect) {
    const std::string text = base::UTF16ToUTF8(title);
    if (text != kExpected || rect != kRect) {
      fprintf(stderr, "embedder: the renderer answered \"%s\" and %s, not \"%s\" and %s\n", text.c_str(),
              rect.ToString().c_str(), kExpected, kRect.ToString().c_str());
      Finish(1);
      return;
    }
    fprintf(stderr, "embedder: the renderer answered %s through probe.mojom\n", text.c_str());
    Finish(ExportsWork() ? 0 : 1);
  }

  // Two targets the SDK exports although they carry no export macros (specs/0001-sdk.md, Exports): Chrome's user
  // agent, and the conversion of UI events into Blink's.
  static bool ExportsWork() {
    const std::string agent = embedder_support::GetUserAgent();
    if (agent.find("Chrome/") == std::string::npos) {
      fprintf(stderr, "embedder: the user agent is \"%s\", with no Chrome/\n", agent.c_str());
      return false;
    }
    const ui::MouseEvent press(ui::EventType::kMousePressed, gfx::PointF(3, 5), gfx::PointF(3, 5),
                               base::TimeTicks::Now(), ui::EF_LEFT_MOUSE_BUTTON, ui::EF_LEFT_MOUSE_BUTTON);
    if (ui::MakeWebMouseEvent(press).GetType() != blink::WebInputEvent::Type::kMouseDown) {
      fprintf(stderr, "embedder: a mouse press didn't become Blink's mouse down\n");
      return false;
    }
    fprintf(stderr, "embedder: the user agent and event conversion work\n");
    return true;
  }

  void OnProbeGone() {
    fprintf(stderr, "embedder: the renderer closed probe.mojom unanswered\n");
    Finish(1);
  }

  // The first outcome is the result: the probe closing as the browser shuts down changes nothing.
  void Finish(int result) {
    if (!done_) {
      return;
    }
    g_result = result;
    std::move(done_).Run();
  }

  base::OnceClosure done_;
  bool asked_ = false;
  mojo::AssociatedRemote<mojom::Probe> probe_;
};

class MainParts : public content::BrowserMainParts {
 public:
  int PreMainMessageLoopRun() override {
    context_ = std::make_unique<BrowserContext>();
    PrepareWindows();
    contents_ = content::WebContents::Create(content::WebContents::CreateParams(context_.get()));
    ShowContents(contents_.get());
    return 0;
  }

  void WillRunMainMessageLoop(std::unique_ptr<base::RunLoop>& run_loop) override {
    check_ = std::make_unique<Check>(contents_.get(), run_loop->QuitClosure());
    contents_->GetController().LoadURLWithParams(content::NavigationController::LoadURLParams(GURL(kPage)));
    base::SingleThreadTaskRunner::GetCurrentDefault()->PostDelayedTask(
        FROM_HERE,
        base::BindOnce(
            [](base::OnceClosure quit) {
              fprintf(stderr, "embedder: no result after %d s\n", static_cast<int>(kTimeout.InSeconds()));
              g_result = 1;
              std::move(quit).Run();
            },
            run_loop->QuitClosure()),
        kTimeout);
  }

  void PostMainMessageLoopRun() override {
    check_.reset();
    contents_.reset();
    HideContents();
    context_.reset();
  }

 private:
  std::unique_ptr<BrowserContext> context_;
  std::unique_ptr<content::WebContents> contents_;
  std::unique_ptr<Check> check_;
};

class BrowserClient : public content::ContentBrowserClient {
 public:
  std::unique_ptr<content::BrowserMainParts> CreateBrowserMainParts(bool) override {
    return std::make_unique<MainParts>();
  }
};

class MainDelegate : public content::ContentMainDelegate {
 public:
  std::optional<int> BasicStartupComplete() override {
    base::CommandLine& command_line = *base::CommandLine::ForCurrentProcess();
    // The GPU process renders with SwiftShader, which every machine has, through ANGLE: the SDK's GPU runtime
    // files are what it loads.
    command_line.AppendSwitchASCII("use-gl", "angle");
    command_line.AppendSwitchASCII("use-angle", "swiftshader");
    command_line.AppendSwitch("enable-unsafe-swiftshader");
    AppendPlatformSwitches(command_line);
    return std::nullopt;
  }

  // Every process loads the SDK's resources and English strings from beside the executable.
  void PreSandboxStartup() override {
    base::FilePath assets;
    CHECK(base::PathService::Get(base::DIR_ASSETS, &assets));
    ui::ResourceBundle::InitSharedInstanceWithPakPath(assets.AppendASCII("locales").AppendASCII("en-US.pak"));
    ui::ResourceBundle::GetSharedInstance().AddDataPackFromPath(assets.AppendASCII("resources.pak"),
                                                                ui::k100Percent);
  }

  std::optional<int> PreBrowserMain() override {
    if (std::optional<int> stop = content::ContentMainDelegate::PreBrowserMain()) {
      return stop;
    }
    return embedder::PreBrowserMain();
  }

  content::ContentClient* CreateContentClient() override {
    content_client_ = std::make_unique<ContentClient>();
    return content_client_.get();
  }

  content::ContentBrowserClient* CreateContentBrowserClient() override {
    browser_client_ = std::make_unique<BrowserClient>();
    return browser_client_.get();
  }

  content::ContentRendererClient* CreateContentRendererClient() override {
    renderer_client_ = std::make_unique<RendererClient>();
    return renderer_client_.get();
  }

 private:
  std::unique_ptr<ContentClient> content_client_;
  std::unique_ptr<BrowserClient> browser_client_;
  std::unique_ptr<RendererClient> renderer_client_;
};

}  // namespace
}  // namespace embedder

int main(int argc, const char** argv) {
  if (const int stop = embedder::EarlyMain(argc, argv); stop >= 0) {
    return stop;
  }
  embedder::MainDelegate delegate;
  content::ContentMainParams params(&delegate);
  params.argc = argc;
  params.argv = argv;
  const int code = content::ContentMain(std::move(params));
  // Only the browser process has a result; a child's code is Content's own.
  bool child = false;
  for (int i = 1; i < argc; ++i) {
    child = child || std::string(argv[i]).rfind("--type=", 0) == 0;
  }
  return child || code != 0 ? code : embedder::g_result;
}
