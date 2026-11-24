#import <AppKit/AppKit.h>
#import <WebKit/WebKit.h>
#include <unistd.h>

static NSString *const WorkspaceLocationKey = @"WorkspaceLocation";

@interface ChemCode : NSObject <NSApplicationDelegate, NSWindowDelegate, WKUIDelegate,
                               WKNavigationDelegate, WKDownloadDelegate>
@property (nonatomic, strong) NSURL *origin;
@property (nonatomic, strong) NSURL *dataDirectory;
@property (nonatomic, strong) NSURL *handshakeDirectory;
@property (nonatomic, copy) NSString *token;
@property (nonatomic, strong) NSTask *worker;
@property (nonatomic, strong) NSFileHandle *workerInput;
@property (nonatomic, strong) NSURLSession *readiness;
@property (nonatomic, strong) NSWindow *window;
@property (nonatomic, strong) WKWebView *webView;
@property (nonatomic, strong) NSMutableArray<NSWindow *> *popups;
@property (nonatomic, strong) NSMapTable<WKDownload *, NSArray<NSURL *> *> *downloads;
@property (nonatomic) BOOL stopping;
@property (nonatomic) BOOL reportedError;
@end

@implementation ChemCode

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    [self installMenus];
    self.popups = [NSMutableArray array];
    self.downloads = [NSMapTable strongToStrongObjectsMapTable];
    self.readiness = [NSURLSession sessionWithConfiguration:NSURLSessionConfiguration.ephemeralSessionConfiguration];
    self.token = [NSUUID.UUID.UUIDString stringByAppendingString:NSUUID.UUID.UUIDString];
    NSDictionary *environment = NSProcessInfo.processInfo.environment;
    NSInteger port = environment[@"CHEM_CODE_DESKTOP_PORT"]
        ? [environment[@"CHEM_CODE_DESKTOP_PORT"] integerValue] : 17895;
    if (port < 1024 || port > 65535) {
        [self startupError:@"本地端口无效。"];
        return;
    }
    self.origin = [NSURL URLWithString:[NSString stringWithFormat:@"http://127.0.0.1:%ld", port]];
    NSFileManager *files = NSFileManager.defaultManager;
    NSString *path = environment[@"CHEM_CODE_DESKTOP_DATA"]
        ?: [NSUserDefaults.standardUserDefaults stringForKey:WorkspaceLocationKey];
    self.dataDirectory = path.length ? [NSURL fileURLWithPath:path isDirectory:YES]
        : [[files URLsForDirectory:NSApplicationSupportDirectory inDomains:NSUserDomainMask].firstObject
            URLByAppendingPathComponent:@"Chem Code" isDirectory:YES];
    self.window = [self makeWindow];
    NSProgressIndicator *indicator = [[NSProgressIndicator alloc] init];
    indicator.style = NSProgressIndicatorStyleSpinning;
    indicator.translatesAutoresizingMaskIntoConstraints = NO;
    [self.window.contentView addSubview:indicator];
    [NSLayoutConstraint activateConstraints:@[
        [indicator.centerXAnchor constraintEqualToAnchor:self.window.contentView.centerXAnchor],
        [indicator.centerYAnchor constraintEqualToAnchor:self.window.contentView.centerYAnchor],
    ]];
    [indicator startAnimation:nil];
    [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];

    NSError *error = nil;
    if (![self startWorker:environment error:&error]) {
        [self startupError:error.localizedDescription];
        return;
    }
    [self checkWorkspaceUntil:[NSDate dateWithTimeIntervalSinceNow:25]];
}

- (BOOL)startWorker:(NSDictionary *)environment error:(NSError **)error {
    NSFileManager *files = NSFileManager.defaultManager;
    if (![files createDirectoryAtURL:self.dataDirectory withIntermediateDirectories:YES
                          attributes:@{NSFilePosixPermissions: @0700} error:error]) return NO;
    self.handshakeDirectory = [files.temporaryDirectory URLByAppendingPathComponent:NSUUID.UUID.UUIDString];
    if (![files createDirectoryAtURL:self.handshakeDirectory withIntermediateDirectories:NO
                          attributes:@{NSFilePosixPermissions: @0700} error:error]) return NO;

    NSURL *log = [self.dataDirectory URLByAppendingPathComponent:@"service.log"];
    NSNumber *size = nil;
    [log getResourceValue:&size forKey:NSURLFileSizeKey error:nil];
    if (size.unsignedLongLongValue > 5000000) {
        NSURL *previous = [self.dataDirectory URLByAppendingPathComponent:@"service.previous.log"];
        [files removeItemAtURL:previous error:nil];
        if (![files moveItemAtURL:log toURL:previous error:error]) return NO;
    }
    if (![files fileExistsAtPath:log.path]) {
        [files createFileAtPath:log.path contents:nil attributes:@{NSFilePosixPermissions: @0600}];
    }
    NSFileHandle *stream = [NSFileHandle fileHandleForWritingToURL:log error:error];
    if (!stream || ![stream seekToEndReturningOffset:NULL error:error]) return NO;

    NSTask *worker = [[NSTask alloc] init];
    worker.executableURL = [NSBundle.mainBundle.resourceURL URLByAppendingPathComponent:@"Python/bin/python3.11"];
    worker.arguments = @[@"-I", @"-B", @"-m", @"chemistry_agent.desktop"];
    worker.currentDirectoryURL = self.dataDirectory;
    NSString *home = files.homeDirectoryForCurrentUser.path;
    NSArray *paths = @[[home stringByAppendingPathComponent:@".local/bin"],
        [home stringByAppendingPathComponent:@".cargo/bin"],
        [home stringByAppendingPathComponent:@"Applications/Codex.app/Contents/Resources"],
        @"/opt/homebrew/bin",
        @"/usr/local/bin", @"/Applications/Codex.app/Contents/Resources",
        @"/usr/bin", @"/bin", @"/usr/sbin"];
    NSMutableDictionary *child = [environment mutableCopy];
    NSString *path = [paths componentsJoinedByString:@":"];
    if ([environment[@"PATH"] length]) path = [path stringByAppendingFormat:@":%@", environment[@"PATH"]];
    child[@"PATH"] = path;
    child[@"PYTHONDONTWRITEBYTECODE"] = @"1";
    child[@"CHEM_CODE_DESKTOP_DATA"] = self.dataDirectory.path;
    child[@"CHEM_CODE_DESKTOP_PORT"] = self.origin.port.stringValue;
    child[@"CHEM_CODE_DESKTOP_TOKEN"] = self.token;
    child[@"CHEM_CODE_DESKTOP_READY_FILE"] = [self.handshakeDirectory URLByAppendingPathComponent:@"bound"].path;
    worker.environment = child;
    NSPipe *pipe = [NSPipe pipe];
    worker.standardInput = pipe;
    worker.standardOutput = stream;
    worker.standardError = stream;
    __weak ChemCode *owner = self;
    worker.terminationHandler = ^(NSTask *task) {
        dispatch_async(dispatch_get_main_queue(), ^{ [owner workerExited]; });
    };
    BOOL started = [worker launchAndReturnError:error];
    [pipe.fileHandleForReading closeAndReturnError:NULL];
    [stream closeAndReturnError:NULL];
    if (started) {
        self.worker = worker;
        self.workerInput = pipe.fileHandleForWriting;
    }
    return started;
}

- (NSURLRequest *)launchRequest:(NSString *)endpoint {
    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:
        [self.origin URLByAppendingPathComponent:endpoint]
        cachePolicy:NSURLRequestReloadIgnoringLocalCacheData timeoutInterval:2];
    [request setValue:self.token forHTTPHeaderField:@"X-Chem-Code-Launch"];
    return request;
}

- (void)checkWorkspaceUntil:(NSDate *)deadline {
    if (self.stopping || !self.worker.running) return;
    if (deadline.timeIntervalSinceNow <= 0) {
        [self startupError:@"本地服务启动超时。"];
        return;
    }
    if (![NSFileManager.defaultManager fileExistsAtPath:
            [self.handshakeDirectory URLByAppendingPathComponent:@"bound"].path]) {
        [self retryWorkspaceUntil:deadline];
        return;
    }
    [[self.readiness dataTaskWithRequest:[self launchRequest:@"__desktop/ready"]
        completionHandler:^(NSData *body, NSURLResponse *response, NSError *error) {
        dispatch_async(dispatch_get_main_queue(), ^{
            if (self.stopping || !self.worker.running) return;
            NSHTTPURLResponse *http = (NSHTTPURLResponse *)response;
            if (!error && http.statusCode == 200 && [body isEqualToData:[@"ready" dataUsingEncoding:NSUTF8StringEncoding]]) {
                [self removeHandshake];
                WKWebViewConfiguration *configuration = [self workspaceWebConfiguration];
                if (!configuration) return;
                self.webView = [self makeWebView:configuration];
                self.window.contentView = self.webView;
                [self.webView loadRequest:[self launchRequest:@"__desktop/start"]];
            } else {
                [self retryWorkspaceUntil:deadline];
            }
        });
    }] resume];
}

- (void)retryWorkspaceUntil:(NSDate *)deadline {
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 200 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{
        [self checkWorkspaceUntil:deadline];
    });
}

- (void)removeHandshake {
    if (self.handshakeDirectory) {
        [NSFileManager.defaultManager removeItemAtURL:self.handshakeDirectory error:nil];
        self.handshakeDirectory = nil;
    }
}

- (void)workerExited {
    [self removeHandshake];
    [self.workerInput closeFile];
    self.workerInput = nil;
    self.worker = nil;
    if (self.stopping) {
        [NSApp replyToApplicationShouldTerminate:YES];
    } else {
        [self startupError:[NSString stringWithFormat:@"本地服务已退出。若端口 %@ 已被占用，请查看日志。", self.origin.port]];
    }
}

- (void)startupError:(NSString *)detail {
    if (self.reportedError) return;
    self.reportedError = YES;
    [self removeHandshake];
    NSAlert *alert = [[NSAlert alloc] init];
    alert.messageText = @"Chem Code 无法运行";
    alert.informativeText = detail ?: @"本地服务未能启动。";
    [alert addButtonWithTitle:@"退出"];
    if (self.dataDirectory) [alert addButtonWithTitle:@"查看日志"];
    if ([alert runModal] == NSAlertSecondButtonReturn) {
        [[NSWorkspace sharedWorkspace] activateFileViewerSelectingURLs:
            @[[self.dataDirectory URLByAppendingPathComponent:@"service.log"]]];
    }
    [NSApp terminate:nil];
}

- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)sender {
    NSTask *worker = self.worker;
    if (!worker.running) return NSTerminateNow;
    self.stopping = YES;
    [self.workerInput closeFile];
    self.workerInput = nil;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 12 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
        if (worker.running) kill(worker.processIdentifier, SIGKILL);
    });
    return NSTerminateLater;
}

- (BOOL)applicationShouldHandleReopen:(NSApplication *)sender hasVisibleWindows:(BOOL)visible {
    [self.window makeKeyAndOrderFront:nil];
    return YES;
}

- (NSWindow *)makeWindow {
    NSWindow *window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 1280, 820)
        styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable backing:NSBackingStoreBuffered defer:NO];
    window.contentMinSize = NSMakeSize(390, 520);
    window.title = @"Chem Code";
    window.releasedWhenClosed = NO;
    window.delegate = self;
    [window center];
    return window;
}

- (WKWebView *)makeWebView:(WKWebViewConfiguration *)configuration {
    WKWebView *view = [[WKWebView alloc] initWithFrame:NSZeroRect configuration:configuration];
    view.navigationDelegate = self;
    view.UIDelegate = self;
    view.allowsBackForwardNavigationGestures = YES;
    return view;
}

- (WKWebViewConfiguration *)workspaceWebConfiguration {
    NSURL *marker = [self.dataDirectory URLByAppendingPathComponent:@"webkit-store-id"];
    NSError *error = nil;
    NSString *stored = [NSString stringWithContentsOfURL:marker encoding:NSUTF8StringEncoding error:&error];
    NSUUID *identifier = stored ? [[NSUUID alloc] initWithUUIDString:stored] : nil;
    if (!identifier && error.code == NSFileReadNoSuchFileError) {
        identifier = NSUUID.UUID;
        if (![identifier.UUIDString writeToURL:marker atomically:YES encoding:NSUTF8StringEncoding error:&error]) {
            [self startupError:error.localizedDescription];
            return nil;
        }
    }
    if (!identifier) {
        [self startupError:@"无法读取当前工作区的浏览器数据标识。"];
        return nil;
    }
    WKWebViewConfiguration *configuration = [[WKWebViewConfiguration alloc] init];
    configuration.websiteDataStore = [WKWebsiteDataStore dataStoreForIdentifier:identifier];
    return configuration;
}

- (BOOL)isLocal:(NSURL *)url {
    return [url.scheme isEqual:self.origin.scheme] && [url.host isEqual:self.origin.host]
        && [url.port isEqual:self.origin.port];
}

- (void)webView:(WKWebView *)view decidePolicyForNavigationAction:(WKNavigationAction *)action
        decisionHandler:(void (^)(WKNavigationActionPolicy))decision {
    NSURL *url = action.request.URL;
    if ([self isLocal:url] || [@[@"about", @"blob", @"data"] containsObject:url.scheme ?: @""]) {
        decision(action.shouldPerformDownload ? WKNavigationActionPolicyDownload : WKNavigationActionPolicyAllow);
        return;
    }
    if ((action.navigationType == WKNavigationTypeLinkActivated || !action.targetFrame)
        && [@[@"http", @"https", @"mailto"] containsObject:url.scheme ?: @""]) {
        [NSWorkspace.sharedWorkspace openURL:url];
    }
    decision(WKNavigationActionPolicyCancel);
}

- (void)webView:(WKWebView *)view decidePolicyForNavigationResponse:(WKNavigationResponse *)response
        decisionHandler:(void (^)(WKNavigationResponsePolicy))decision {
    NSHTTPURLResponse *http = [response.response isKindOfClass:NSHTTPURLResponse.class]
        ? (NSHTTPURLResponse *)response.response : nil;
    BOOL attachment = [[[http valueForHTTPHeaderField:@"Content-Disposition"] lowercaseString]
        hasPrefix:@"attachment"];
    decision(!response.canShowMIMEType || attachment ? WKNavigationResponsePolicyDownload : WKNavigationResponsePolicyAllow);
}

- (void)webView:(WKWebView *)view didFailProvisionalNavigation:(WKNavigation *)navigation
       withError:(NSError *)error {
    if (view == self.webView && !self.stopping && !view.URL && error.code != NSURLErrorCancelled) {
        [self startupError:error.localizedDescription];
    }
}

- (WKWebView *)webView:(WKWebView *)view createWebViewWithConfiguration:(WKWebViewConfiguration *)configuration
        forNavigationAction:(WKNavigationAction *)action windowFeatures:(WKWindowFeatures *)features {
    if (![self isLocal:action.request.URL]) return nil;
    NSWindow *window = [self makeWindow];
    WKWebView *child = [self makeWebView:configuration];
    window.contentView = child;
    [self.popups addObject:window];
    [window makeKeyAndOrderFront:nil];
    return child;
}

- (void)windowWillClose:(NSNotification *)notification {
    [self.popups removeObjectIdenticalTo:notification.object];
}

- (void)webViewDidClose:(WKWebView *)view {
    if (view != self.webView) [view.window close];
}

- (void)webView:(WKWebView *)view runOpenPanelWithParameters:(WKOpenPanelParameters *)parameters
        initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(NSArray<NSURL *> *))completion {
    NSOpenPanel *panel = [NSOpenPanel openPanel];
    panel.allowsMultipleSelection = parameters.allowsMultipleSelection;
    panel.canChooseDirectories = parameters.allowsDirectories;
    panel.canChooseFiles = !parameters.allowsDirectories;
    [panel beginSheetModalForWindow:view.window completionHandler:^(NSModalResponse result) {
        completion(result == NSModalResponseOK ? panel.URLs : nil);
    }];
}

- (void)webView:(WKWebView *)view runJavaScriptConfirmPanelWithMessage:(NSString *)message
        initiatedByFrame:(WKFrameInfo *)frame completionHandler:(void (^)(BOOL))completion {
    NSAlert *alert = [[NSAlert alloc] init];
    alert.messageText = message;
    [alert addButtonWithTitle:@"确定"];
    [alert addButtonWithTitle:@"取消"];
    [alert beginSheetModalForWindow:view.window completionHandler:^(NSModalResponse result) {
        completion(result == NSAlertFirstButtonReturn);
    }];
}

- (void)webView:(WKWebView *)view navigationAction:(WKNavigationAction *)action didBecomeDownload:(WKDownload *)download {
    download.delegate = self;
}

- (void)webView:(WKWebView *)view navigationResponse:(WKNavigationResponse *)response didBecomeDownload:(WKDownload *)download {
    download.delegate = self;
}

- (void)download:(WKDownload *)download decideDestinationUsingResponse:(NSURLResponse *)response
        suggestedFilename:(NSString *)filename completionHandler:(void (^)(NSURL *))completion {
    NSSavePanel *panel = [NSSavePanel savePanel];
    panel.nameFieldStringValue = filename;
    NSWindow *window = NSApp.keyWindow ?: self.window;
    [panel beginSheetModalForWindow:window completionHandler:^(NSModalResponse result) {
        if (result != NSModalResponseOK) {
            completion(nil);
            return;
        }
        NSError *error = nil;
        NSURL *directory = [NSFileManager.defaultManager URLForDirectory:NSItemReplacementDirectory
            inDomain:NSUserDomainMask appropriateForURL:panel.URL create:YES error:&error];
        if (!directory) {
            [NSApp presentError:error];
            completion(nil);
            return;
        }
        NSURL *source = [directory URLByAppendingPathComponent:@"download"];
        [self.downloads setObject:@[directory, source, panel.URL] forKey:download];
        completion(source);
    }];
}

- (void)downloadDidFinish:(WKDownload *)download {
    NSArray<NSURL *> *transfer = [self.downloads objectForKey:download];
    if (!transfer) return;
    NSError *error = nil;
    NSFileManager *files = NSFileManager.defaultManager;
    NSURL *source = transfer[1], *destination = transfer[2];
    if ([files fileExistsAtPath:destination.path]) {
        [files replaceItemAtURL:destination withItemAtURL:source backupItemName:nil
                       options:0 resultingItemURL:NULL error:&error];
    } else {
        [files moveItemAtURL:source toURL:destination error:&error];
    }
    [files removeItemAtURL:transfer[0] error:nil];
    [self.downloads removeObjectForKey:download];
    if (error) [NSApp presentError:error];
}

- (void)download:(WKDownload *)download didFailWithError:(NSError *)error resumeData:(NSData *)resumeData {
    NSURL *directory = [self.downloads objectForKey:download].firstObject;
    if (directory) [NSFileManager.defaultManager removeItemAtURL:directory error:nil];
    [self.downloads removeObjectForKey:download];
    if (error.code != NSURLErrorCancelled) [NSApp presentError:error];
}

- (void)showSettings:(id)sender {
    [self.window makeKeyAndOrderFront:nil];
    [self.webView loadRequest:[NSURLRequest requestWithURL:[self.origin URLByAppendingPathComponent:@"settings"]]];
}

- (void)showData:(id)sender {
    if (self.dataDirectory) [NSWorkspace.sharedWorkspace openURL:self.dataDirectory];
}

- (void)chooseDataLocation:(id)sender {
    NSOpenPanel *panel = [NSOpenPanel openPanel];
    panel.canChooseFiles = NO;
    panel.canChooseDirectories = YES;
    panel.canCreateDirectories = YES;
    panel.prompt = @"选择上一级文件夹";
    panel.directoryURL = [self.dataDirectory URLByDeletingLastPathComponent];
    panel.message = @"下次打开时，Chem Code 会使用所选文件夹里的独立工作区。现有数据会留在原位置。";
    [self.window makeKeyAndOrderFront:nil];
    [panel beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse result) {
        if (result != NSModalResponseOK) return;
        NSFileManager *files = NSFileManager.defaultManager;
        NSURL *location = [[panel.URL URLByAppendingPathComponent:@"Chem Code" isDirectory:YES]
            URLByResolvingSymlinksInPath];
        for (NSString *component in location.pathComponents) {
            if ([component caseInsensitiveCompare:@"archive"] == NSOrderedSame) {
                [self showLocationMessage:@"请选择 archive 目录之外的位置。"];
                return;
            }
        }
        NSError *error = nil;
        if (![files createDirectoryAtURL:location withIntermediateDirectories:YES
                              attributes:@{NSFilePosixPermissions: @0700} error:&error]) {
            [NSApp presentError:error];
            return;
        }
        NSDictionary *attributes = [files attributesOfItemAtPath:location.path error:&error];
        if (!attributes) {
            [NSApp presentError:error];
            return;
        }
        if ([attributes[NSFileOwnerAccountID] unsignedIntValue] != getuid()
            || ([attributes[NSFilePosixPermissions] unsignedIntValue] & 0077) != 0) {
            [self showLocationMessage:@"请选择当前 macOS 用户独占的文件夹。"];
            return;
        }
        [NSUserDefaults.standardUserDefaults setObject:location.path forKey:WorkspaceLocationKey];
        [self showLocationMessage:@"新位置已保存，下次打开 Chem Code 时生效。"];
    }];
}

- (void)restoreDataLocation:(id)sender {
    [NSUserDefaults.standardUserDefaults removeObjectForKey:WorkspaceLocationKey];
    [self showLocationMessage:@"下次打开时使用「应用程序支持」里的默认工作区。"];
}

- (void)showLocationMessage:(NSString *)message {
    NSAlert *alert = [[NSAlert alloc] init];
    alert.messageText = message;
    [alert beginSheetModalForWindow:self.window completionHandler:nil];
}

- (NSMenu *)addMenu:(NSString *)title to:(NSMenu *)bar {
    NSMenuItem *item = [[NSMenuItem alloc] init];
    item.submenu = [[NSMenu alloc] initWithTitle:title];
    [bar addItem:item];
    return item.submenu;
}

- (void)installMenus {
    NSMenu *bar = [[NSMenu alloc] init];
    NSMenu *app = [self addMenu:@"Chem Code" to:bar];
    [app addItemWithTitle:@"关于 Chem Code" action:@selector(orderFrontStandardAboutPanel:) keyEquivalent:@""];
    [app addItemWithTitle:@"设置…" action:@selector(showSettings:) keyEquivalent:@","].target = self;
    [app addItemWithTitle:@"打开数据目录" action:@selector(showData:) keyEquivalent:@""].target = self;
    [app addItemWithTitle:@"更改工作区位置…" action:@selector(chooseDataLocation:) keyEquivalent:@""].target = self;
    [app addItemWithTitle:@"恢复默认工作区位置" action:@selector(restoreDataLocation:) keyEquivalent:@""].target = self;
    [app addItem:NSMenuItem.separatorItem];
    [app addItemWithTitle:@"退出 Chem Code" action:@selector(terminate:) keyEquivalent:@"q"];
    NSMenu *edit = [self addMenu:@"编辑" to:bar];
    [edit addItemWithTitle:@"撤销" action:@selector(undo:) keyEquivalent:@"z"];
    [edit addItemWithTitle:@"重做" action:@selector(redo:) keyEquivalent:@"z"].keyEquivalentModifierMask =
        NSEventModifierFlagCommand | NSEventModifierFlagShift;
    [edit addItemWithTitle:@"剪切" action:@selector(cut:) keyEquivalent:@"x"];
    [edit addItemWithTitle:@"复制" action:@selector(copy:) keyEquivalent:@"c"];
    [edit addItemWithTitle:@"粘贴" action:@selector(paste:) keyEquivalent:@"v"];
    [edit addItemWithTitle:@"全选" action:@selector(selectAll:) keyEquivalent:@"a"];
    NSMenu *windows = [self addMenu:@"窗口" to:bar];
    [windows addItemWithTitle:@"关闭窗口" action:@selector(performClose:) keyEquivalent:@"w"];
    [windows addItemWithTitle:@"最小化" action:@selector(performMiniaturize:) keyEquivalent:@"m"];
    NSApp.windowsMenu = windows;
    NSApp.mainMenu = bar;
}

@end

int main(void) {
    @autoreleasepool {
        NSApplication *app = NSApplication.sharedApplication;
        ChemCode *delegate = [[ChemCode alloc] init];
        app.delegate = delegate;
        [app setActivationPolicy:NSApplicationActivationPolicyRegular];
        [app run];
        app.delegate = nil;
    }
    return 0;
}
