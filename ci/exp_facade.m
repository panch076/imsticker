// Experiment: use the Objective-C entry points Apple's own apps use for
// stickers, to see what they return for stickers this project put in a store,
// and what Apple's code itself stores when handed an image.
//
//     signatures                                    method signatures of the entry points
//     fetch <uuid>...                               ask for stickers by identifier
//     touch <uuid>                                  mark a sticker as used
//     donate <uuid> <name> <uri or -> <uti> <width> <height> <role or -> <file>
//     stickerkit <file> <width> <height> [add]      what StickerKit builds from image data
//
// Stand-alone, the entry points talk to stickersd over XPC. Linked into
// exp_apple_store.swift, they are pointed at a store opened in this process.
//
// Build: clang -fobjc-arc -framework Foundation -framework CoreGraphics ci/exp_facade.m -o exp_facade

#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>
#import <dlfcn.h>
#import <objc/runtime.h>
#import <objc/message.h>
#import <unistd.h>

int STKExperimentRun(id store, NSArray<NSString *> *arguments);

// Declared so the compiler knows this is an initialiser; the class that
// implements it is _STKStickerUIStickerRepresentation, found at run time.
@interface NSObject (StickerRepresentationInitialiser)
- (instancetype)initWithData:(NSData *)data type:(NSString *)type size:(CGSize)size role:(NSString *)role;
@end

static void say(NSString *format, ...) NS_FORMAT_FUNCTION(1, 2);
static void say(NSString *format, ...) {
    va_list arguments;
    va_start(arguments, format);
    NSString *line = [[NSString alloc] initWithFormat:format arguments:arguments];
    va_end(arguments);
    printf("%s\n", line.UTF8String);
    fflush(stdout);
}

static NSString *describe(id value) {
    return value == nil ? @"nil" : [NSString stringWithFormat:@"%@", value];
}

static id valueOrNote(id object, NSString *key) {
    @try {
        return [object valueForKey:key];
    } @catch (NSException *exception) {
        return @"(not readable)";
    }
}

static void printSignatures(NSString *className) {
    Class cls = NSClassFromString(className);
    if (cls == Nil) {
        say(@"%@: no such class", className);
        return;
    }
    for (int pass = 0; pass < 2; pass++) {
        unsigned int count = 0;
        Method *methods = class_copyMethodList(pass == 0 ? cls : object_getClass(cls), &count);
        for (unsigned int i = 0; i < count; i++) {
            const char *types = method_getTypeEncoding(methods[i]);
            say(@"%@  %c%s  %s", className, pass == 0 ? '-' : '+', sel_getName(method_getName(methods[i])), types ? types : "");
        }
        free(methods);
    }
}

static void printRepresentation(id item, NSString *indent) {
    NSData *data = valueOrNote(item, @"data");
    id uti = valueOrNote(item, @"uti");
    id type = [uti isKindOfClass:[NSString class]] ? uti : valueOrNote(uti, @"identifier");
    CGSize size = CGSizeZero;
    id boxed = valueOrNote(item, @"size");
    if ([boxed isKindOfClass:[NSValue class]]) {
        [boxed getValue:&size size:sizeof(size)];
    }
    NSMutableString *line = [NSMutableString stringWithFormat:@"%@role=%@ type=%@ size=%.0fx%.0f bytes=%lu",
                             indent, describe(valueOrNote(item, @"role")), describe(type), size.width, size.height,
                             (unsigned long)([data isKindOfClass:[NSData class]] ? data.length : 0)];
    for (NSString *key in @[@"isPreferred", @"version", @"byteCount"]) {
        if ([item respondsToSelector:NSSelectorFromString(key)]) {
            [line appendFormat:@" %@=%@", key, describe(valueOrNote(item, key))];
        }
    }
    if ([data isKindOfClass:[NSData class]] && data.length >= 12) {
        const unsigned char *bytes = data.bytes;
        [line appendFormat:@" starts=%02x%02x%02x%02x %c%c%c%c%c%c%c%c", bytes[0], bytes[1], bytes[2], bytes[3],
         bytes[4] > 31 ? bytes[4] : '.', bytes[5] > 31 ? bytes[5] : '.', bytes[6] > 31 ? bytes[6] : '.', bytes[7] > 31 ? bytes[7] : '.',
         bytes[8] > 31 ? bytes[8] : '.', bytes[9] > 31 ? bytes[9] : '.', bytes[10] > 31 ? bytes[10] : '.', bytes[11] > 31 ? bytes[11] : '.'];
    }
    say(@"%@", line);
}

static void printSticker(id sticker) {
    say(@"  sticker %@ (%s)", describe(valueOrNote(sticker, @"identifier")), object_getClassName(sticker));
    for (NSString *key in @[@"name", @"externalURI", @"effectType", @"effect", @"accessibilityName", @"searchText",
                            @"byteCount", @"creationDate", @"libraryIndex", @"isGeneratedSticker"]) {
        if ([sticker respondsToSelector:NSSelectorFromString(key)]) {
            say(@"    %@ = %@", key, describe(valueOrNote(sticker, key)));
        }
    }
    say(@"    description = %@", describe(valueOrNote(sticker, @"description")));
    NSArray *representations = valueOrNote(sticker, @"representations");
    if ([representations isKindOfClass:[NSArray class]]) {
        say(@"    representations: %lu", (unsigned long)representations.count);
        for (id item in representations) {
            printRepresentation(item, @"      ");
        }
    }
}

// Build one of Apple's facade objects. When a store opened in this process was
// handed in, put it where the facade keeps the one it would reach over XPC.
static id makeFacade(NSString *className, id store) {
    Class cls = NSClassFromString(className);
    if (cls == Nil) {
        say(@"%@: no such class", className);
        return nil;
    }
    id facade = [[cls alloc] init];
    if (facade == nil || store == nil) {
        return facade;
    }
    Ivar ivar = class_getInstanceVariable(cls, "store");
    if (ivar == NULL) {
        say(@"%@ keeps no 'store'; using it as it is", className);
        return facade;
    }
    ptrdiff_t offset = ivar_getOffset(ivar);
    size_t words = (class_getInstanceSize(cls) - (size_t)offset) / sizeof(void *);
    void **slot = (void **)((char *)(__bridge void *)facade + offset);
    NSMutableString *layout = [NSMutableString string];
    for (size_t i = 0; i < words && i < 6; i++) {
        [layout appendFormat:@" %p", slot[i]];
    }
    say(@"%@: 'store' is at +%td, with %zu words to the end of the object:%@", className, offset, words, layout);
    void *existing = slot[0];
    if (existing == NULL) {
        say(@"  nothing there to replace; using the facade as it is");
        return facade;
    }
    const char *theirs = object_getClassName((__bridge id)existing);
    const char *ours = object_getClassName(store);
    say(@"  it holds a %s; the store opened here is a %s", theirs, ours);
    if (strcmp(theirs, ours) != 0) {
        say(@"  different classes, so not replacing it");
        return facade;
    }
    slot[0] = (__bridge_retained void *)store;  // the one it had is leaked, on purpose
    say(@"  replaced with the store opened in this process");
    return facade;
}

static int commandFetch(id store, NSArray<NSString *> *arguments) {
    id facade = makeFacade(@"_STKTextInputObjCStoreFacade", store);
    SEL selector = NSSelectorFromString(@"stickersWithIdentifiers:roles:error:");
    if (facade == nil || ![facade respondsToSelector:selector]) {
        say(@"fetch: the entry point is missing");
        return 1;
    }
    NSMutableArray *identifiers = [NSMutableArray array];
    for (NSString *text in [arguments subarrayWithRange:NSMakeRange(1, arguments.count - 1)]) {
        NSUUID *uuid = [[NSUUID alloc] initWithUUIDString:text];
        if (uuid != nil) {
            [identifiers addObject:uuid];
        }
    }
    NSArray *roles = @[@"com.apple.stickers.role.still", @"com.apple.stickers.role.keyboard",
                       @"com.apple.stickers.role.animated", @"com.apple.stickers.role.original",
                       @"com.apple.stickers.role.stillVariant_320"];
    NSError *error = nil;
    id result = ((id (*)(id, SEL, id, id, NSError **))objc_msgSend)(facade, selector, identifiers, roles, &error);
    say(@"fetch: asked for %lu, error=%@", (unsigned long)identifiers.count, describe(error));
    if ([result isKindOfClass:[NSArray class]]) {
        say(@"fetch: got %lu stickers", (unsigned long)[result count]);
        for (id sticker in result) {
            printSticker(sticker);
        }
    } else {
        say(@"fetch: result = %@", describe(result));
    }
    return 0;
}

static int commandTouch(id store, NSArray<NSString *> *arguments) {
    id facade = makeFacade(@"_STKTextInputObjCStoreFacade", store);
    SEL selector = NSSelectorFromString(@"touchStickerWithIdentifier:error:");
    NSUUID *uuid = [[NSUUID alloc] initWithUUIDString:arguments[1]];
    if (facade == nil || uuid == nil || ![facade respondsToSelector:selector]) {
        say(@"touch: not possible");
        return 1;
    }
    NSError *error = nil;
    BOOL ok = ((BOOL (*)(id, SEL, id, NSError **))objc_msgSend)(facade, selector, uuid, &error);
    say(@"touch: ok=%d error=%@", ok, describe(error));
    return 0;
}

static int commandDonate(id store, NSArray<NSString *> *arguments) {
    NSString *identifier = arguments[1];
    NSString *name = arguments[2];
    NSString *uri = [arguments[3] isEqualToString:@"-"] ? nil : arguments[3];
    NSString *uti = arguments[4];
    CGSize size = CGSizeMake(arguments[5].doubleValue, arguments[6].doubleValue);
    NSString *role = [arguments[7] isEqualToString:@"-"] ? nil : arguments[7];
    NSData *data = [NSData dataWithContentsOfFile:arguments[8]];
    Class representationClass = NSClassFromString(@"_STKStickerUIStickerRepresentation");
    if (data == nil || representationClass == Nil
        || ![representationClass instancesRespondToSelector:@selector(initWithData:type:size:role:)]) {
        say(@"donate: missing input or classes");
        return 1;
    }
    id representation = [[representationClass alloc] initWithData:data type:uti size:size role:role];
    say(@"donate: representation = %@", describe(representation));
    id facade = makeFacade(@"_STKMessagesObjCStoreFacade", store);
    SEL selector = NSSelectorFromString(
        @"donateStickerToRecentsWithIdentifier:representations:stickerEffectEnum:externalURI:name:accessibilityName:metadata:attributionInfo:error:");
    if (representation == nil || facade == nil || ![facade respondsToSelector:selector]) {
        say(@"donate: the entry point is missing");
        return 1;
    }
    NSError *error = nil;
    @try {
        // The identifier is a string here, unlike in the fetch entry point.
        BOOL ok = ((BOOL (*)(id, SEL, id, id, NSInteger, id, id, id, id, id, NSError **))objc_msgSend)(
            facade, selector, identifier, @[representation], 0, uri, name, name, nil, nil, &error);
        say(@"donate: ok=%d error=%@", ok, describe(error));
    } @catch (NSException *exception) {
        say(@"donate: raised %@: %@", exception.name, exception.reason);
    }
    return 0;
}

static int commandStickerKit(NSArray<NSString *> *arguments) {
    if (dlopen("/System/Library/Frameworks/StickerKit.framework/StickerKit", RTLD_NOW) == NULL) {
        say(@"stickerkit: cannot load StickerKit: %s", dlerror());
        return 1;
    }
    Class storeClass = NSClassFromString(@"STKStickerStore");
    NSData *data = [NSData dataWithContentsOfFile:arguments[1]];
    CGSize size = CGSizeMake(arguments[2].doubleValue, arguments[3].doubleValue);
    SEL makeRepresentation = NSSelectorFromString(@"stickerRepresentationWithData:size:");
    SEL makeSticker = NSSelectorFromString(@"stickerWithRepresentation:identifier:isRecent:");
    if (storeClass == Nil || data == nil || ![storeClass respondsToSelector:makeRepresentation]
        || ![storeClass respondsToSelector:makeSticker]) {
        say(@"stickerkit: STKStickerStore or its class methods are missing");
        return 1;
    }
    printSignatures(@"STKStickerStore");
    id representation = ((id (*)(id, SEL, id, CGSize))objc_msgSend)(storeClass, makeRepresentation, data, size);
    say(@"stickerkit: representation from %@ = %s", arguments[1].lastPathComponent, representation ? object_getClassName(representation) : "nil");
    if (representation == nil) {
        return 0;
    }
    printRepresentation(representation, @"  ");
    NSUUID *uuid = [NSUUID UUID];
    id identifiers[2] = {uuid, uuid.UUIDString};
    for (int recent = 0; recent < 2; recent++) {
        id sticker = nil;
        for (int form = 0; form < 2 && sticker == nil; form++) {
            @try {
                sticker = ((id (*)(id, SEL, id, id, BOOL))objc_msgSend)(storeClass, makeSticker, representation, identifiers[form], recent == 1);
            } @catch (NSException *exception) {
                say(@"stickerkit: identifier as %s raised %@", form == 0 ? "NSUUID" : "NSString", exception.reason);
            }
        }
        say(@"stickerkit: sticker with isRecent=%d:", recent);
        if (sticker != nil) {
            printSticker(sticker);
        }
        if (recent == 0 && sticker != nil && [arguments containsObject:@"add"]) {
            id kitStore = [[storeClass alloc] init];
            SEL add = NSSelectorFromString(@"addSticker:");
            if ([kitStore respondsToSelector:add]) {
                BOOL ok = ((BOOL (*)(id, SEL, id))objc_msgSend)(kitStore, add, sticker);
                say(@"stickerkit: -[STKStickerStore addSticker:] returned %d", ok);
            }
        }
    }
    return 0;
}

int STKExperimentRun(id store, NSArray<NSString *> *arguments) {
    @autoreleasepool {
        alarm(90);  // never hang the job
        if (arguments.count < 1) {
            say(@"usage: signatures | fetch <uuid>... | touch <uuid> | donate ... | stickerkit <file> <w> <h> [add]");
            return 2;
        }
        if (dlopen("/System/Library/PrivateFrameworks/Stickers.framework/Stickers", RTLD_NOW) == NULL) {
            say(@"cannot load Stickers.framework: %s", dlerror());
            return 1;
        }
        NSString *command = arguments[0];
        if ([command isEqualToString:@"signatures"]) {
            for (NSString *name in @[@"_STKTextInputObjCStoreFacade", @"_STKMessagesObjCStoreFacade",
                                     @"_STKStickerUIStickerRepresentation"]) {
                printSignatures(name);
            }
            return 0;
        }
        if ([command isEqualToString:@"fetch"] && arguments.count >= 2) {
            return commandFetch(store, arguments);
        }
        if ([command isEqualToString:@"touch"] && arguments.count >= 2) {
            return commandTouch(store, arguments);
        }
        if ([command isEqualToString:@"donate"] && arguments.count >= 9) {
            return commandDonate(store, arguments);
        }
        if ([command isEqualToString:@"stickerkit"] && arguments.count >= 4) {
            return commandStickerKit(arguments);
        }
        say(@"unknown command or missing arguments: %@", [arguments componentsJoinedByString:@" "]);
        return 2;
    }
}

#ifndef EXP_NO_MAIN
int main(int argc, const char *argv[]) {
    NSMutableArray<NSString *> *arguments = [NSMutableArray array];
    for (int i = 1; i < argc; i++) {
        [arguments addObject:@(argv[i])];
    }
    return STKExperimentRun(nil, arguments);
}
#endif
