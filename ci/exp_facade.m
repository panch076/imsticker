// Experiment: talk to the sticker system through the Objective-C entry points
// Apple's own apps use, to see what they return for stickers this project put
// in the store, and what they accept.
//
//     exp_facade signatures
//     exp_facade fetch <uuid>...
//     exp_facade donate <uuid> <name> <uri or -> <uti> <width> <height> <role or -> <file>
//     exp_facade touch <uuid>
//
// Build: clang -fobjc-arc -framework Foundation -framework CoreGraphics ci/exp_facade.m -o exp_facade

#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>
#import <dlfcn.h>
#import <objc/runtime.h>
#import <objc/message.h>
#import <unistd.h>

static NSString *const kFramework = @"/System/Library/PrivateFrameworks/Stickers.framework/Stickers";

// Declared so the compiler knows this is an initialiser; the class that
// implements it is _STKStickerUIStickerRepresentation, found at run time.
@interface NSObject (StickerRepresentationInitialiser)
- (instancetype)initWithData:(NSData *)data type:(NSString *)type size:(CGSize)size role:(NSString *)role;
@end

static void printSignatures(NSString *className) {
    Class cls = NSClassFromString(className);
    if (cls == Nil) {
        printf("%s: no such class\n", className.UTF8String);
        return;
    }
    unsigned int count = 0;
    Method *methods = class_copyMethodList(cls, &count);
    for (unsigned int i = 0; i < count; i++) {
        const char *types = method_getTypeEncoding(methods[i]);
        printf("%s  -%s  %s\n", className.UTF8String, sel_getName(method_getName(methods[i])), types ? types : "");
    }
    free(methods);
}

static char returnKind(id object, SEL selector) {
    Method method = class_getInstanceMethod([object class], selector);
    if (method == NULL) {
        return 0;
    }
    char buffer[8] = {0};
    method_getReturnType(method, buffer, sizeof(buffer));
    return buffer[0];
}

static NSString *describe(id value) {
    if (value == nil) {
        return @"nil";
    }
    return [NSString stringWithFormat:@"%@", value];
}

static void printSticker(id sticker) {
    printf("  sticker %s\n", describe([sticker valueForKey:@"identifier"]).UTF8String);
    for (NSString *key in @[@"name", @"externalURI", @"effectType", @"accessibilityName", @"searchText"]) {
        @try {
            printf("    %s = %s\n", key.UTF8String, describe([sticker valueForKey:key]).UTF8String);
        } @catch (NSException *exception) {
            printf("    %s: (not readable)\n", key.UTF8String);
        }
    }
    NSArray *representations = [sticker valueForKey:@"representations"];
    printf("    representations: %lu\n", (unsigned long)representations.count);
    for (id item in representations) {
        NSData *data = [item valueForKey:@"data"];
        id uti = [item valueForKey:@"uti"];
        NSValue *size = [item valueForKey:@"size"];
        CGSize dimensions = CGSizeZero;
        [size getValue:&dimensions size:sizeof(dimensions)];
        printf("      role=%s uti=%s size=%.0fx%.0f bytes=%lu\n",
               describe([item valueForKey:@"role"]).UTF8String,
               describe([uti valueForKey:@"identifier"]).UTF8String,
               dimensions.width, dimensions.height, (unsigned long)data.length);
    }
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        alarm(90);  // never hang the job
        if (argc < 2) {
            printf("usage: exp_facade signatures | fetch <uuid>... | donate ... | touch <uuid>\n");
            return 2;
        }
        if (dlopen(kFramework.UTF8String, RTLD_NOW) == NULL) {
            printf("cannot load %s: %s\n", kFramework.UTF8String, dlerror());
            return 1;
        }
        NSString *command = @(argv[1]);

        if ([command isEqualToString:@"signatures"]) {
            for (NSString *name in @[@"_STKTextInputObjCStoreFacade", @"_STKMessagesObjCStoreFacade",
                                     @"_STKStickerUIStickerRepresentation", @"_STKStickerAttributionInfo"]) {
                printSignatures(name);
            }
            return 0;
        }

        if ([command isEqualToString:@"fetch"]) {
            Class cls = NSClassFromString(@"_STKTextInputObjCStoreFacade");
            if (cls == Nil) {
                printf("fetch: the text-input facade class is missing\n");
                return 1;
            }
            id facade = [[cls alloc] init];
            NSMutableArray *identifiers = [NSMutableArray array];
            for (int i = 2; i < argc; i++) {
                NSUUID *uuid = [[NSUUID alloc] initWithUUIDString:@(argv[i])];
                if (uuid != nil) {
                    [identifiers addObject:uuid];
                }
            }
            NSArray *roles = @[@"com.apple.stickers.role.still", @"com.apple.stickers.role.keyboard",
                               @"com.apple.stickers.role.animated", @"com.apple.stickers.role.original"];
            SEL selector = NSSelectorFromString(@"stickersWithIdentifiers:roles:error:");
            if (![facade respondsToSelector:selector]) {
                printf("fetch: the facade does not respond to stickersWithIdentifiers:roles:error:\n");
                return 1;
            }
            NSError *error = nil;
            id result = ((id (*)(id, SEL, id, id, NSError **))objc_msgSend)(facade, selector, identifiers, roles, &error);
            printf("fetch: asked for %lu, error=%s\n", (unsigned long)identifiers.count, describe(error).UTF8String);
            if ([result isKindOfClass:[NSArray class]]) {
                printf("fetch: got %lu stickers\n", (unsigned long)[result count]);
                for (id sticker in result) {
                    printSticker(sticker);
                }
            } else {
                printf("fetch: result = %s\n", describe(result).UTF8String);
            }
            return 0;
        }

        if ([command isEqualToString:@"touch"] && argc >= 3) {
            id facade = [[NSClassFromString(@"_STKTextInputObjCStoreFacade") alloc] init];
            SEL selector = NSSelectorFromString(@"touchStickerWithIdentifier:error:");
            NSUUID *uuid = [[NSUUID alloc] initWithUUIDString:@(argv[2])];
            if (facade == nil || uuid == nil || ![facade respondsToSelector:selector]) {
                printf("touch: not possible\n");
                return 1;
            }
            NSError *error = nil;
            if (returnKind(facade, selector) == '@') {
                id result = ((id (*)(id, SEL, id, NSError **))objc_msgSend)(facade, selector, uuid, &error);
                printf("touch: result=%s error=%s\n", describe(result).UTF8String, describe(error).UTF8String);
            } else {
                BOOL ok = ((BOOL (*)(id, SEL, id, NSError **))objc_msgSend)(facade, selector, uuid, &error);
                printf("touch: ok=%d error=%s\n", ok, describe(error).UTF8String);
            }
            return 0;
        }

        if ([command isEqualToString:@"donate"] && argc >= 10) {
            NSUUID *uuid = [[NSUUID alloc] initWithUUIDString:@(argv[2])];
            NSString *name = @(argv[3]);
            NSString *uri = strcmp(argv[4], "-") == 0 ? nil : @(argv[4]);
            NSString *uti = @(argv[5]);
            CGSize size = CGSizeMake(atof(argv[6]), atof(argv[7]));
            NSString *role = strcmp(argv[8], "-") == 0 ? nil : @(argv[8]);
            NSData *data = [NSData dataWithContentsOfFile:@(argv[9])];
            Class representationClass = NSClassFromString(@"_STKStickerUIStickerRepresentation");
            Class facadeClass = NSClassFromString(@"_STKMessagesObjCStoreFacade");
            if (uuid == nil || data == nil || representationClass == Nil || facadeClass == Nil) {
                printf("donate: missing input or classes\n");
                return 1;
            }
            if (![representationClass instancesRespondToSelector:@selector(initWithData:type:size:role:)]) {
                printf("donate: the representation class has no initWithData:type:size:role:\n");
                return 1;
            }
            id representation = [[representationClass alloc] initWithData:data type:uti size:size role:role];
            printf("donate: representation = %s\n", describe(representation).UTF8String);
            id facade = [[facadeClass alloc] init];
            SEL selector = NSSelectorFromString(
                @"donateStickerToRecentsWithIdentifier:representations:stickerEffectEnum:externalURI:name:accessibilityName:metadata:attributionInfo:error:");
            if (representation == nil || ![facade respondsToSelector:selector]) {
                printf("donate: the facade does not respond to the donate selector\n");
                return 1;
            }
            NSError *error = nil;
            if (returnKind(facade, selector) == '@') {
                id result = ((id (*)(id, SEL, id, id, NSInteger, id, id, id, id, id, NSError **))objc_msgSend)(
                    facade, selector, uuid, @[representation], 0, uri, name, name, nil, nil, &error);
                printf("donate: result=%s error=%s\n", describe(result).UTF8String, describe(error).UTF8String);
            } else {
                BOOL ok = ((BOOL (*)(id, SEL, id, id, NSInteger, id, id, id, id, id, NSError **))objc_msgSend)(
                    facade, selector, uuid, @[representation], 0, uri, name, name, nil, nil, &error);
                printf("donate: ok=%d error=%s\n", ok, describe(error).UTF8String);
            }
            return 0;
        }

        printf("unknown command or missing arguments\n");
        return 2;
    }
}
