Below is the full, *evidence-backed* path from the changed code to every HTTP (and other) entrypoint it can affect.

---

## 1. Changed code location and behavior

**File:** `src/modules/user/domain/value-objects/address.value-object.ts`

The change adds a US‑specific postal code rule inside `Address.validate`:

```ts
export class Address extends ValueObject<AddressProps> {
  // ...

  protected validate(props: AddressProps): void {
    if (!Guard.lengthIsBetween(props.country, 2, 50)) {
      throw new ArgumentOutOfRangeException('country is out of range');
    }
    if (!Guard.lengthIsBetween(props.street, 2, 50)) {
      throw new ArgumentOutOfRangeException('street is out of range');
    }
    if (!Guard.lengthIsBetween(props.postalCode, 2, 10)) {
      throw new ArgumentOutOfRangeException('postalCode is out of range');
    }
+    /* US postal codes must be a 5-digit ZIP code (e.g. "10001").
+     * Country-specific postal code formats vary widely; this is a
+     * deliberately narrow, deterministic example rule for the US only. */
+    if (props.country === 'US' && !/^\d{5}$/.test(props.postalCode)) {
+      throw new ArgumentOutOfRangeException(
+        'postalCode is out of range: US postal code must match ^\\d{5}$',
+      );
+    }
  }
}
```

Because `Address` extends `ValueObject<T>`, its constructor always calls `validate`:

- `ValueObject` ctor: `this.validate(props);`  
  **File:** `src/libs/ddd/value-object.base.ts`  
  ```ts
  export abstract class ValueObject<T> {
    protected readonly props: ValueObjectProps<T>;

    constructor(props: ValueObjectProps<T>) {
      this.checkIfEmpty(props);
      this.validate(props);          // <- calls Address.validate
      this.props = props;
    }

    protected abstract validate(props: ValueObjectProps<T>): void;
  }
  ```

So any code path that does `new Address(...)` can now throw an `ArgumentOutOfRangeException` when `country === 'US'` and `postalCode` doesn’t match `/^\d{5}$/`.

---

## 2. Where `Address` is constructed

### 2.1. In `CreateUserService` (create user command handler)

**File:** `src/modules/user/commands/create-user/create-user.service.ts`

```ts
import { Address } from '@modules/user/domain/value-objects/address.value-object';

@CommandHandler(CreateUserCommand)
export class CreateUserService implements ICommandHandler {
  // ...

  async execute(
    command: CreateUserCommand,
  ): Promise<Result<AggregateID, UserAlreadyExistsError>> {
    const user = UserEntity.create({
      email: command.email,
      address: new Address({         // <- constructs Address
        country: command.country,
        postalCode: command.postalCode,
        street: command.street,
      }),
    });
    // ...
  }
}
```

This path is clearly affected: any client providing US/country + invalid postalCode to the *create user* command will now trigger the stricter `Address.validate`.

### 2.2. In `UserEntity.updateAddress` (updating address in the domain)

**File:** `src/modules/user/domain/user.entity.ts`

```ts
import { Address, AddressProps } from './value-objects/address.value-object';

export class UserEntity extends AggregateRoot<UserProps> {
  // ...

  updateAddress(props: UpdateUserAddressProps): void {
    const newAddress = new Address({
      ...this.props.address,
      ...props,
    } as AddressProps);                 // <- constructs Address again

    this.props.address = newAddress;

    this.addEvent(
      new UserAddressUpdatedDomainEvent({
        aggregateId: this.id,
        country: newAddress.country,
        street: newAddress.street,
        postalCode: newAddress.postalCode,
      }),
    );
  }
}
```

Any caller of `UserEntity.updateAddress` is therefore also affected by the new validation rule. In the files provided, there is **no controller/handler invoking `updateAddress`**; the only call site is inside `UserEntity` itself. So:

- The *potential* impact exists.
- But there is no HTTP or other entrypoint in the given files that actually calls `updateAddress`. I cannot assert a real chain to an entrypoint without additional files.

### 2.3. In `UserMapper.toDomain` (hydrating entities from DB)

**File:** `src/modules/user/user.mapper.ts`

```ts
import { Address } from './domain/value-objects/address.value-object';

@Injectable()
export class UserMapper
  implements Mapper<UserEntity, UserModel, UserResponseDto>
{
  // ...

  toDomain(record: UserModel): UserEntity {
    const entity = new UserEntity({
      id: record.id,
      createdAt: new Date(record.createdAt),
      updatedAt: new Date(record.updatedAt),
      props: {
        email: record.email,
        role: record.role,
        address: new Address({          // <- constructs Address from DB record
          street: record.street,
          postalCode: record.postalCode,
          country: record.country,
        }),
      },
    });
    return entity;
  }
}
```

Here, existing data in the `users` table is revalidated by the `Address` rules. Any HTTP or GraphQL path that loads `UserEntity` via a repository using `UserMapper.toDomain` can now start throwing if a stored US postal code does not match the new regex.

We therefore must trace where:

- `UserMapper.toDomain` is used, and
- where `UserRepository` (which uses this mapper) is invoked by services reachable from entrypoints.

---

## 3. Repository and mapper usage

### 3.1. `UserRepository` extends `SqlRepositoryBase` and uses `UserMapper`

**File:** `src/modules/user/database/user.repository.ts`

```ts
@Injectable()
export class UserRepository
  extends SqlRepositoryBase<UserEntity, UserModel>
  implements UserRepositoryPort
{
  protected tableName = 'users';

  protected schema = userSchema;

  constructor(
    @InjectPool()
    pool: DatabasePool,
    mapper: UserMapper,            // <- injected mapper
    eventEmitter: EventEmitter2,
  ) {
    super(pool, mapper, eventEmitter, new Logger(UserRepository.name));
  }

  async updateAddress(user: UserEntity): Promise<void> {
    const address = user.getProps().address;
    const statement = sql.type(userSchema)`
    UPDATE "users" SET
    street = ${address.street}, country = ${address.country}, "postalCode" = ${address.postalCode}
    WHERE id = ${user.id}`;

    await this.writeQuery(statement, user);
  }

  async findOneByEmail(email: string): Promise<UserEntity> {
    const user = await this.pool.one(
      sql.type(userSchema)`SELECT * FROM "users" WHERE email = ${email}`,
    );

    return this.mapper.toDomain(user);  // <- calls UserMapper.toDomain
  }
}
```

We do **not** have `SqlRepositoryBase` in the provided files, so we cannot see exactly where else `mapper.toDomain` is used (e.g., in `findOneById`, `findAll`, etc.). The interface suggests it might:

- `RepositoryPort<Entity>` in `src/libs/ddd/repository.port.ts` declares methods like `findOneById`, `findAll`, etc., but does not show implementations.

Since `SqlRepositoryBase` is not included, I cannot assert specific additional `toDomain` callsites beyond the explicit `findOneByEmail` method shown.

### 3.2. Where `UserRepositoryPort` is used

`UserRepositoryPort` is imported in:

#### a) `CreateUserService` (already covered)

**File:** `src/modules/user/commands/create-user/create-user.service.ts`

```ts
import { UserRepositoryPort } from '@modules/user/database/user.repository.port';
import { Inject } from '@nestjs/common';
import { USER_REPOSITORY } from '../../user.di-tokens';

@CommandHandler(CreateUserCommand)
export class CreateUserService implements ICommandHandler {
  constructor(
    @Inject(USER_REPOSITORY)
    protected readonly userRepo: UserRepositoryPort,   // <- injection
  ) {}
  // uses .transaction and .insert, not find methods
}
```

Note: In this file, the repository is only used to `insert` the `UserEntity` inside a `transaction`; no read (`findOneByEmail`, `findOneById`, etc.) is invoked, so the `UserMapper.toDomain`–caused validation isn’t used along this path.

#### b) `DeleteUserService`

**File:** `src/modules/user/commands/delete-user/delete-user.service.ts`

```ts
import { UserRepositoryPort } from '@modules/user/database/user.repository.port';
import { Inject } from '@nestjs/common';
import { CommandHandler } from '@nestjs/cqrs';
import { USER_REPOSITORY } from '../../user.di-tokens';

@CommandHandler(DeleteUserCommand)
export class DeleteUserService {
  constructor(
    @Inject(USER_REPOSITORY)
    private readonly userRepo: UserRepositoryPort,  // <- injection
  ) {}

  async execute(
    command: DeleteUserCommand,
  ): Promise<Result<boolean, NotFoundException>> {
    const found = await this.userRepo.findOneById(command.userId); // <- repository read
    if (found.isNone()) return Err(new NotFoundException());
    const user = found.unwrap();
    user.delete();
    const result = await this.userRepo.delete(user);
    return Ok(result);
  }
}
```

- `findOneById` comes from `RepositoryPort` (see `src/libs/ddd/repository.port.ts`), whose concrete implementation is presumably in `SqlRepositoryBase`. That code is not provided, so we cannot *see* that `mapper.toDomain` is used here, but since `SqlRepositoryBase` is constructed with `mapper: UserMapper` in `UserRepository` (above), it is *likely*.  
- However, the instructions require only literal, visible evidence. Therefore:

  - It **is certain** that `DeleteUserService` calls `userRepo.findOneById(...)`.
  - It is **not visible** in supplied code whether that call leads to `new Address(...)` via `UserMapper.toDomain`. I must explicitly say: this step is probable but not proven from the given files.

What we *can* assert is that the delete flow **will rehydrate** a `UserEntity` (type `UserEntity`) from the repository. That entity, per `UserEntity`’s definition, contains an `Address` value object:

- `UserProps` includes `address: Address`  
  **File:** `src/modules/user/domain/user.types.ts`  
  ```ts
  export interface UserProps {
    role: UserRoles;
    email: string;
    address: Address;   // <- address is part of the entity props
  }
  ```

- `UserEntity` extends `AggregateRoot<UserProps>`, which extends `Entity<UserProps>`:

  **File:** `src/libs/ddd/entity.base.ts`  
  ```ts
  constructor({
    id,
    createdAt,
    updatedAt,
    props,
  }: CreateEntityProps<EntityProps>) {
    this.setId(id);
    this.validateProps(props);
    const now = new Date();
    this._createdAt = createdAt || now;
    this._updatedAt = updatedAt || now;
    this.props = props;
    this.validate();
  }
  ```

This constructor does **not** construct the `Address`; it assumes `props.address` is already an `Address`. On the read path, that `Address` would have to be created somewhere—`UserMapper.toDomain` is the only such location visible. So the delete flow is indirectly dependent on `Address` instantiation, but that internal call chain isn’t explicitly visible without `SqlRepositoryBase`.

Given the constraints, I will not assert the implicit `mapper.toDomain` call in `findOneById`.

---

## 4. Command & query buses, controllers, and entrypoints

Now we connect HTTP (and other) entrypoints to the affected `Address` usage through the NestJS CQRS patterns and DI wiring.

### 4.1. NestJS module wiring

**File:** `src/modules/user/user.module.ts`

```ts
import { UserRepository } from './database/user.repository';
import { CreateUserHttpController } from './commands/create-user/create-user.http.controller';
import { DeleteUserHttpController } from './commands/delete-user/delete-user.http-controller';
import { CreateUserCliController } from './commands/create-user/create-user.cli.controller';
import { FindUsersHttpController } from './queries/find-users/find-users.http.controller';
import { CreateUserMessageController } from './commands/create-user/create-user.message.controller';
import { CreateUserGraphqlResolver } from './commands/create-user/graphql-example/create-user.graphql-resolver';
import { CreateUserService } from './commands/create-user/create-user.service';
import { DeleteUserService } from './commands/delete-user/delete-user.service';
import { FindUsersQueryHandler } from './queries/find-users/find-users.query-handler';
import { UserMapper } from './user.mapper';
import { CqrsModule } from '@nestjs/cqrs';
import { USER_REPOSITORY } from './user.di-tokens';
import { FindUsersGraphqlResolver } from './queries/find-users/find-users.graphql-resolver';

const httpControllers = [
  CreateUserHttpController,
  DeleteUserHttpController,
  FindUsersHttpController,
];

const messageControllers = [CreateUserMessageController];

const cliControllers: Provider[] = [CreateUserCliController];

const graphqlResolvers: Provider[] = [
  CreateUserGraphqlResolver,
  FindUsersGraphqlResolver,
];

const commandHandlers: Provider[] = [CreateUserService, DeleteUserService];

const queryHandlers: Provider[] = [FindUsersQueryHandler];

const mappers: Provider[] = [UserMapper];

const repositories: Provider[] = [
  { provide: USER_REPOSITORY, useClass: UserRepository },
];

@Module({
  imports: [CqrsModule],
  controllers: [...httpControllers, ...messageControllers],
  providers: [
    Logger,
    ...cliControllers,
    ...repositories,
    ...graphqlResolvers,
    ...commandHandlers,
    ...queryHandlers,
    ...mappers,
  ],
})
export class UserModule {}
```

This shows:

- `CreateUserService` and `DeleteUserService` are registered as `commandHandlers` under `CqrsModule`.
- `UserRepository` is bound to `USER_REPOSITORY`.
- HTTP controllers, message controllers, CLI controllers, and GraphQL resolvers are all wired here.

NestJS CQRS uses the `@CommandHandler` decorator to bind command classes to their handlers; we can see those annotations:

- `@CommandHandler(CreateUserCommand)` on `CreateUserService`  
  **File:** `src/modules/user/commands/create-user/create-user.service.ts`

- `@CommandHandler(DeleteUserCommand)` on `DeleteUserService`  
  **File:** `src/modules/user/commands/delete-user/delete-user.service.ts`

And controllers/resolvers leverage `CommandBus` and `QueryBus` to dispatch commands/queries to these handlers.

---

## 5. Full call chains from entrypoints to `Address` usage

### 5.1. HTTP: Create user

**Entrypoint:** `POST {routesV1.version}{routesV1.user.root}`

- Defined by `@Controller(routesV1.version)` and `@Post(routesV1.user.root)`.

**File:** `src/modules/user/commands/create-user/create-user.http.controller.ts`

```ts
@Controller(routesV1.version)
export class CreateUserHttpController {
  constructor(private readonly commandBus: CommandBus) {}

  @Post(routesV1.user.root)
  async create(@Body() body: CreateUserRequestDto): Promise<IdResponse> {
    const command = new CreateUserCommand(body);  // <- construct command

    const result: Result<AggregateID, UserAlreadyExistsError> =
      await this.commandBus.execute(command);     // <- dispatch to handler

    return match(result, {
      Ok: (id: string) => new IdResponse(id),
      Err: (error: Error) => {
        if (error instanceof UserAlreadyExistsError)
          throw new ConflictHttpException(error.message);
        throw error;
      },
    });
  }
}
```

**Validation at DTO level:** `CreateUserRequestDto` constrains the input:

**File:** `src/modules/user/commands/create-user/create-user.request.dto.ts`

```ts
export class CreateUserRequestDto {
  @IsEmail()
  readonly email: string;

  @Matches(/^[a-zA-Z ]*$/)
  readonly country: string;

  @MaxLength(10)
  @MinLength(4)
  @IsAlphanumeric()
  readonly postalCode: string;

  @Matches(/^[a-zA-Z ]*$/)
  readonly street: string;
}
```

These are class-validator rules applied by Nest when binding `@Body()` to the DTO (standard NestJS behavior, not shown directly in code here but implied by using DTO with decorators).

**Next hop:** `CreateUserCommand` constructor:

**File:** `src/modules/user/commands/create-user/create-user.command.ts`

```ts
import { Command, CommandProps } from '@libs/ddd';

export class CreateUserCommand extends Command {
  readonly email: string;
  readonly country: string;
  readonly postalCode: string;
  readonly street: string;

  constructor(props: CommandProps<CreateUserCommand>) {
    super(props);                   // <- Command base ctor
    this.email = props.email;
    this.country = props.country;
    this.postalCode = props.postalCode;
    this.street = props.street;
  }
}
```

**Underlying base command behavior:**

**File:** `src/libs/ddd/command.base.ts`

```ts
export class Command {
  constructor(props: CommandProps<unknown>) {
    if (Guard.isEmpty(props)) {
      throw new ArgumentNotProvidedException(
        'Command props should not be empty',
      );
    }
    const ctx = RequestContextService.getContext();
    this.id = props.id || randomUUID();
    this.metadata = {
      correlationId: props?.metadata?.correlationId || ctx.requestId,
      causationId: props?.metadata?.causationId,
      timestamp: props?.metadata?.timestamp || Date.now(),
      userId: props?.metadata?.userId,
    };
  }
}
```

This step is not directly modified, but is part of the chain.

**Dispatching through CommandBus to handler:**

- We do not see `CommandBus.execute` internals, but we do see handler registration:

  - `@CommandHandler(CreateUserCommand)`  
    **File:** `src/modules/user/commands/create-user/create-user.service.ts`  

  - `CreateUserService` provided in `UserModule` as a provider, and `CqrsModule` imported with `CommandBus`.

So it is justified to say: `commandBus.execute(command)` results in `CreateUserService.execute` being called for a `CreateUserCommand`.

**Handler logic leading to `Address`:**

**File:** `src/modules/user/commands/create-user/create-user.service.ts`

```ts
@CommandHandler(CreateUserCommand)
export class CreateUserService implements ICommandHandler {
  constructor(
    @Inject(USER_REPOSITORY)
    protected readonly userRepo: UserRepositoryPort,
  ) {}

  async execute(
    command: CreateUserCommand,
  ): Promise<Result<AggregateID, UserAlreadyExistsError>> {
    const user = UserEntity.create({
      email: command.email,
      address: new Address({
        country: command.country,
        postalCode: command.postalCode,
        street: command.street,
      }),
    });
    // ...
  }
}
```

**UserEntity.create** invokes `Address.unpack()` only *after* `Address` is constructed and validated:

**File:** `src/modules/user/domain/user.entity.ts`

```ts
static create(create: CreateUserProps): UserEntity {
  const id = randomUUID();
  const props: UserProps = { ...create, role: UserRoles.guest };
  const user = new UserEntity({ id, props });
  user.addEvent(
    new UserCreatedDomainEvent({
      aggregateId: id,
      email: props.email,
      ...props.address.unpack(),      // <- uses Address (post-validation)
    }),
  );
  return user;
}
```

The crucial step is the `new Address({...})` in the handler, because the new US‑specific validation is executed in the `Address` constructor via `ValueObject` base.

**Therefore, the full, evidenced path for HTTP create-user is:**

1. HTTP `POST` to `routesV1.version + routesV1.user.root`  
   - `@Controller(routesV1.version)` + `@Post(routesV1.user.root)`  
   - **File:** `create-user.http.controller.ts`

2. Nest binds and validates body into `CreateUserRequestDto`  
   - `@Body() body: CreateUserRequestDto` with validation decorators  
   - **File:** `create-user.request.dto.ts`

3. `CreateUserHttpController.create` constructs `CreateUserCommand(body)`  
   - **File:** `create-user.http.controller.ts`

4. `CommandBus.execute(command)` dispatches to the registered handler  
   - `@CommandHandler(CreateUserCommand)` on `CreateUserService`  
   - `UserModule` includes `CreateUserService` in `commandHandlers` and imports `CqrsModule`  
   - **Files:** `create-user.service.ts`, `user.module.ts`

5. `CreateUserService.execute` constructs `new Address({ country, postalCode, street })`  
   - **File:** `create-user.service.ts`

6. `Address` constructor (via `ValueObject` base) calls `Address.validate`  
   - `ValueObject` ctor calls `this.validate(props);`  
   - `Address.validate` contains the new US postal code rule  
   - **Files:** `value-object.base.ts`, `address.value-object.ts`

7. If validation passes, execution continues (user is created, repo persists it); if not, an `ArgumentOutOfRangeException` is thrown.

**Effect:** this HTTP entrypoint is **definitely affected** by the change.

---

### 5.2. CLI: Create user

**Entrypoint:** CLI command `new user <email> <country> <postalCode> <street>`

**File:** `src/modules/user/commands/create-user/create-user.cli.controller.ts`

```ts
@Console({
  command: 'new',
  description: 'A command to create a user',
})
export class CreateUserCliController {
  constructor(
    private readonly commandBus: CommandBus,
    @Inject(Logger)
    private readonly logger: LoggerPort,
  ) {}

  @Command({
    command: 'user <email> <country> <postalCode> <street>',
    description: 'Create a user',
  })
  async createUser(
    email: string,
    country: string,
    postalCode: string,
    street: string,
  ): Promise<void> {
    const command = new CreateUserCommand({
      email,
      country,
      postalCode,
      street,
    });

    const result = await this.commandBus.execute(command);

    this.logger.log('User created:', result.unwrap());
  }
}
```

The subsequent chain is identical from the `CreateUserCommand` onwards:

- `CreateUserCommand` → `CommandBus.execute` → `CreateUserService.execute` → `new Address(...)` → `Address.validate(...)` (with US rule).

So the CLI entrypoint is also **definitely affected**.

---

### 5.3. Message (microservice): Create user

**Entrypoint:** Message pattern `'user.create'` via Nest microservices

**File:** `src/modules/user/commands/create-user/create-user.message.controller.ts`

```ts
@Controller()
export class CreateUserMessageController {
  constructor(private readonly commandBus: CommandBus) {}

  @MessagePattern('user.create') // <- Subscribe to a microservice message
  async create(message: CreateUserRequestDto): Promise<IdResponse> {
    const command = new CreateUserCommand(message);

    const id = await this.commandBus.execute(command);

    return new IdResponse(id.unwrap());
  }
}
```

Here:

1. A microservice message `'user.create'` is received (not HTTP).
2. `CreateUserCommand(message)` is constructed with the payload.
3. `CommandBus.execute` dispatches to `CreateUserService.execute` (as before).
4. `CreateUserService.execute` calls `new Address({...})`.

Thus, this microservice entrypoint is **definitely affected**.

---

### 5.4. GraphQL: Create user

**Entrypoint:** GraphQL mutation `create(input: CreateUserGqlRequestDto)` returning `IdGqlResponse`.

**File:** `src/modules/user/commands/create-user/graphql-example/create-user.graphql-resolver.ts`

```ts
@Resolver()
export class CreateUserGraphqlResolver {
  constructor(private readonly commandBus: CommandBus) {}

  @Mutation(() => IdGqlResponse)
  async create(
    @Args('input') input: CreateUserGqlRequestDto,
  ): Promise<IdGqlResponse> {
    const command = new CreateUserCommand(input);

    const id: Result<AggregateID, UserAlreadyExistsError> =
      await this.commandBus.execute(command);

    return new IdGqlResponse(id.unwrap());
  }
}
```

Again:

- `CreateUserCommand(input)` → `CommandBus.execute` → `CreateUserService.execute` → `new Address({ ... })` → `Address.validate`.

So this GraphQL mutation is **definitely affected**.

---

### 5.5. HTTP: Delete user

**Entrypoint:** `DELETE {routesV1.version}{routesV1.user.delete}`

**File:** `src/modules/user/commands/delete-user/delete-user.http-controller.ts`

```ts
@Controller(routesV1.version)
export class DeleteUserHttpController {
  constructor(private readonly commandBus: CommandBus) {}

  @Delete(routesV1.user.delete)
  async deleteUser(@Param('id') id: string): Promise<void> {
    const command = new DeleteUserCommand({ userId: id });
    const result: Result<boolean, NotFoundException> =
      await this.commandBus.execute(command);
    // ...
  }
}
```

Then:

**File:** `src/modules/user/commands/delete-user/delete-user.service.ts`

```ts
@CommandHandler(DeleteUserCommand)
export class DeleteUserService {
  constructor(
    @Inject(USER_REPOSITORY)
    private readonly userRepo: UserRepositoryPort,
  ) {}

  async execute(
    command: DeleteUserCommand,
  ): Promise<Result<boolean, NotFoundException>> {
    const found = await this.userRepo.findOneById(command.userId);  // <- read
    if (found.isNone()) return Err(new NotFoundException());
    const user = found.unwrap();
    user.delete();
    const result = await this.userRepo.delete(user);
    return Ok(result);
  }
}
```

The only *visible* call here is `userRepo.findOneById`. Its implementation is not shown, but because:

- `UserRepository` extends `SqlRepositoryBase<UserEntity, UserModel>` and passes `UserMapper` to the base class constructor, and
- `UserMapper.toDomain` constructs `new Address(...)`,

it is highly likely that `findOneById` will at some point call `UserMapper.toDomain`, and thus `new Address(...)`, thus applying the new validation against US postal codes stored in the DB.

However, since `SqlRepositoryBase` and the body of `findOneById` are not provided, we cannot point at the actual `mapper.toDomain` call site for `findOneById`, so:

- I **cannot definitively state** that this HTTP delete route is affected, strictly per the “visible-evidence” rule.
- I can say: if `findOneById` uses `UserMapper.toDomain` (as strongly implied by its type and the `UserRepository` constructor), then a delete request for a user whose stored `country` is `'US'` and whose `postalCode` does not match `/^\d{5}$/` would now throw during entity hydration.

---

### 5.6. HTTP/GraphQL: Find users

The “find users” queries **do not** construct `Address` at all; they operate on raw `UserModel` read from the DB.

#### HTTP Find Users

**File:** `src/modules/user/queries/find-users/find-users.http.controller.ts`

```ts
@Controller(routesV1.version)
export class FindUsersHttpController {
  constructor(private readonly queryBus: QueryBus) {}

  @Get(routesV1.user.root)
  async findUsers(
    @Body() request: FindUsersRequestDto,
    @Query() queryParams: PaginatedQueryRequestDto,
  ): Promise<UserPaginatedResponseDto> {
    const query = new FindUsersQuery({
      ...request,
      limit: queryParams?.limit,
      page: queryParams?.page,
    });
    const result: Result<
      Paginated<UserModel>,
      Error
    > = await this.queryBus.execute(query);
    const paginated = result.unwrap();

    return new UserPaginatedResponseDto({
      ...paginated,
      data: paginated.data.map((user) => ({
        ...new ResponseBase(user),
        email: user.email,
        country: user.country,
        street: user.street,
        postalCode: user.postalCode,
      })),
    });
  }
}
```

#### GraphQL Find Users

**File:** `src/modules/user/queries/find-users/find-users.graphql-resolver.ts`

```ts
@Resolver()
export class FindUsersGraphqlResolver {
  constructor(private readonly queryBus: QueryBus) {}
  @Query(() => UserPaginatedGraphqlResponseDto)
  async findUsers(
    @Args('options', { type: () => String })
    options: PaginatedParams<FindUsersQuery>,
  ): Promise<UserPaginatedGraphqlResponseDto> {
    const query = new FindUsersQuery(options);
    const result: Result<
      Paginated<UserModel>,
      Error
    > = await this.queryBus.execute(query);
    const paginated = result.unwrap();
    const response = new UserPaginatedGraphqlResponseDto({
      ...paginated,
      data: paginated.data.map((user) => ({
        ...new ResponseBase(user),
        email: user.email,
        country: user.country,
        street: user.street,
        postalCode: user.postalCode,
      })),
    });
    return response;
  }
}
```

Both use `FindUsersQueryHandler`:

**File:** `src/modules/user/queries/find-users/find-users.query-handler.ts`

```ts
@QueryHandler(FindUsersQuery)
export class FindUsersQueryHandler implements IQueryHandler {
  constructor(
    @InjectPool()
    private readonly pool: DatabasePool,
  ) {}

  async execute(
    query: FindUsersQuery,
  ): Promise<Result<Paginated<UserModel>, Error>> {
    const statement = sql.type(userSchema)`
         SELECT *
         FROM users
         WHERE
           ${query.country ? sql`country = ${query.country}` : true} AND
           ${query.street ? sql`street = ${query.street}` : true} AND
           ${query.postalCode ? sql`"postalCode" = ${query.postalCode}` : true}
         LIMIT ${query.limit}
         OFFSET ${query.offset}`;

    const records = await this.pool.query(statement);

    return Ok(
      new Paginated({
        data: records.rows,
        count: records.rowCount,
        limit: query.limit,
        page: query.page,
      }),
    );
  }
}
```

- Note: this code returns `UserModel` rows from the DB directly; it does **not** construct `UserEntity` nor `Address`.
- Therefore, the new `Address` validation is **not** invoked anywhere in these find-users flows.

Conclusively:

- HTTP `GET` find-users: **not affected** by this change.
- GraphQL query find-users: **not affected** by this change.

---

## 6. Summary of affected entrypoints

With strict reference to the provided code:

1. **HTTP – Create user**  
   - `POST` to `routesV1.version + routesV1.user.root`  
   - **File:** `create-user.http.controller.ts`  
   - **Definitely affected** (directly constructs `Address` via `CreateUserService`).

2. **CLI – Create user**  
   - Command: `new user <email> <country> <postalCode> <street>`  
   - **File:** `create-user.cli.controller.ts`  
   - **Definitely affected** (same `CreateUserService` path).

3. **Microservice message – Create user**  
   - Message pattern `'user.create'`  
   - **File:** `create-user.message.controller.ts`  
   - **Definitely affected** (same `CreateUserService` path).

4. **GraphQL – Create user**  
   - Mutation `create(input: CreateUserGqlRequestDto)`  
   - **File:** `create-user.graphql-resolver.ts`  
   - **Definitely affected** (same `CreateUserService` path).

5. **HTTP – Delete user**  
   - `DELETE` `routesV1.version + routesV1.user.delete`  
   - **Files:** `delete-user.http-controller.ts`, `delete-user.service.ts`, `user.repository.ts`, `user.mapper.ts`  
   - **Possibly affected**, but:
     - The only visible call is `userRepo.findOneById`.
     - The implementation of `findOneById` (likely in `SqlRepositoryBase`) is not provided.
     - It is *strongly implied* that it uses `UserMapper.toDomain`, which would construct `new Address(...)` and enforce the new validation on stored data, but we cannot provide a direct code snippet for that hop from the given files.
     - So I cannot assert this impact as certain.

6. **HTTP – Find users** and **GraphQL – Find users**  
   - `GET` find-users, GraphQL `findUsers` query  
   - **Files:** `find-users.http.controller.ts`, `find-users.graphql-resolver.ts`, `find-users.query-handler.ts`  
   - **Not affected**, as they never construct `Address` or `UserEntity` from records; they operate on `UserModel` directly.

---

If you’d like, I can next:

- Enumerate exactly which inputs (combinations of `country`, `postalCode`) will now fail for the affected entrypoints, and how exceptions will propagate in those flows.