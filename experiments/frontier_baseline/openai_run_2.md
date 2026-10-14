Below is the full, *evidence-based* path from the changed code in `Address` to each affected entrypoint, with every hop justified by specific code lines.

---

## 1. Changed code

**File:** `src/modules/user/domain/value-objects/address.value-object.ts`

**Location of change:** inside `Address.validate`:

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
    /* US postal codes must be a 5-digit ZIP code (e.g. "10001").
     * Country-specific postal code formats vary widely; this is a
     * deliberately narrow, deterministic example rule for the US only. */
    if (props.country === 'US' && !/^\d{5}$/.test(props.postalCode)) {
      throw new ArgumentOutOfRangeException(
        'postalCode is out of range: US postal code must match ^\\d{5}$',
      );
    }
  }
}
```

This logic runs whenever an `Address` value object is constructed (because the `ValueObject` base class calls `validate` in its constructor).

---

## 2. How `Address.validate` is invoked

### 2.1. Via `ValueObject` base class constructor

**File:** `src/libs/ddd/value-object.base.ts`

```ts
export abstract class ValueObject<T> {
  protected readonly props: ValueObjectProps<T>;

  constructor(props: ValueObjectProps<T>) {
    this.checkIfEmpty(props);
    this.validate(props);          // <-- calls Address.validate
    this.props = props;
  }

  protected abstract validate(props: ValueObjectProps<T>): void;
}
```

Since `Address` extends `ValueObject<AddressProps>`:

```ts
export class Address extends ValueObject<AddressProps> {
  protected validate(props: AddressProps): void { ... }  // override
}
```

any `new Address(...)` will run `Address.validate` (including the new US ZIP check).

---

## 3. All places where `Address` is constructed

### 3.1. In the Create User command handler

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
      address: new Address({          // <-- Address constructed here
        country: command.country,
        postalCode: command.postalCode,
        street: command.street,
      }),
    });
    // ...
  }
}
```

So any flow that results in `CreateUserService.execute` will be affected by the new validation.

---

### 3.2. In the User update-address method

**File:** `src/modules/user/domain/user.entity.ts`

```ts
import { Address, AddressProps } from './value-objects/address.value-object';

export class UserEntity extends AggregateRoot<UserProps> {
  // ...

  updateAddress(props: UpdateUserAddressProps): void {
    const newAddress = new Address({
      ...this.props.address,
      ...props,
    } as AddressProps);              // <-- Address constructed here

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

Any flow that calls `UserEntity.updateAddress` is affected (we will track usages below).

---

### 3.3. In the User mapper (re-hydrating from DB)

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
        address: new Address({          // <-- Address constructed here
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

This affects any path that loads a `UserEntity` from the database through `UserMapper.toDomain`.

---

## 4. Flows that reach `CreateUserService.execute`

`CreateUserService` is decorated as a NestJS CQRS command handler:

```ts
@CommandHandler(CreateUserCommand)
export class CreateUserService implements ICommandHandler {
  // ...
}
```

The CQRS framework wires this handler to `CommandBus.execute(command)` calls whose runtime type is `CreateUserCommand`. We can show every place where `new CreateUserCommand(...)` is created and passed into `commandBus.execute`.

### 4.1. HTTP: Create User HTTP controller

**Entry point:** HTTP POST route.

**File:** `src/modules/user/commands/create-user/create-user.http.controller.ts`

#### 4.1.1. Route definition (HTTP entrypoint)

```ts
@Controller(routesV1.version)
export class CreateUserHttpController {
  constructor(private readonly commandBus: CommandBus) {}

  @Post(routesV1.user.root)                            // <-- HTTP POST
  async create(@Body() body: CreateUserRequestDto): Promise<IdResponse> {
    const command = new CreateUserCommand(body);       // <-- command constructed

    const result: Result<AggregateID, UserAlreadyExistsError> =
      await this.commandBus.execute(command);          // <-- dispatch via CommandBus

    return match(result, {
      Ok: (id: string) => new IdResponse(id),
      Err: (error: Error) => { ... },
    });
  }
}
```

#### 4.1.2. Command type

**File:** `src/modules/user/commands/create-user/create-user.command.ts`

```ts
import { Command, CommandProps } from '@libs/ddd';

export class CreateUserCommand extends Command {       // <-- concrete Command type
  readonly email: string;
  readonly country: string;
  readonly postalCode: string;
  readonly street: string;

  constructor(props: CommandProps<CreateUserCommand>) {
    super(props);
    this.email = props.email;
    this.country = props.country;
    this.postalCode = props.postalCode;
    this.street = props.street;
  }
}
```

#### 4.1.3. Handler binding

**File:** `src/modules/user/commands/create-user/create-user.service.ts`

```ts
import { CommandHandler, ICommandHandler } from '@nestjs/cqrs';
import { CreateUserCommand } from './create-user.command';

@CommandHandler(CreateUserCommand)                     // <-- bound to CreateUserCommand
export class CreateUserService implements ICommandHandler {
  // ...
}
```

By CQRS convention, `CommandBus.execute(command)` finds the handler whose `@CommandHandler()` is parameterized with that command class.

Thus, **HTTP POST `routesV1.version + routesV1.user.root`** → `CreateUserHttpController.create` → `CommandBus.execute(CreateUserCommand)` → `CreateUserService.execute` → `new Address(...)` → `Address.validate` (including new US-zip rule).

---

### 4.2. CLI: Create User CLI controller

**Entry point:** CLI command via `nestjs-console`, not HTTP, but it uses the same handler chain.

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
    });                                     // <-- CreateUserCommand constructed

    const result = await this.commandBus.execute(command);  // <-- dispatch

    this.logger.log('User created:', result.unwrap());
  }
}
```

Same binding and handler as in 4.1.3, so this CLI entry path also triggers the new Address validation.  
This is not an HTTP route, but it’s an entrypoint affected by the change.

---

### 4.3. Messages: Create User microservice message controller

**Entry point:** MessagePattern `'user.create'` (not HTTP, but included for completeness).

**File:** `src/modules/user/commands/create-user/create-user.message.controller.ts`

```ts
@Controller()
export class CreateUserMessageController {
  constructor(private readonly commandBus: CommandBus) {}

  @MessagePattern('user.create')                      // <-- microservice endpoint
  async create(message: CreateUserRequestDto): Promise<IdResponse> {
    const command = new CreateUserCommand(message);   // <-- command constructed

    const id = await this.commandBus.execute(command); // <-- dispatch

    return new IdResponse(id.unwrap());
  }
}
```

Again, same handler as in 4.1.3.

---

### 4.4. GraphQL: Create User resolver

**Entry point:** GraphQL mutation `create`.

**File:** `src/modules/user/commands/create-user/graphql-example/create-user.graphql-resolver.ts`

```ts
@Resolver()
export class CreateUserGraphqlResolver {
  constructor(private readonly commandBus: CommandBus) {}

  @Mutation(() => IdGqlResponse)                     // <-- GraphQL mutation
  async create(
    @Args('input') input: CreateUserGqlRequestDto,
  ): Promise<IdGqlResponse> {
    const command = new CreateUserCommand(input);    // <-- command constructed

    const id: Result<AggregateID, UserAlreadyExistsError> =
      await this.commandBus.execute(command);        // <-- dispatch

    return new IdGqlResponse(id.unwrap());
  }
}
```

Same command → same handler → same `new Address(...)` as 3.1.

---

## 5. Flows that reach `UserEntity.updateAddress` (and thus `new Address`)

We saw in 3.2 that `updateAddress` constructs a new `Address`. We now look for any code that *invokes* `updateAddress`.

Search in the provided files:

- `updateAddress` is defined in `user.entity.ts` (shown).
- There is no other file in the bundle calling `user.updateAddress(...)`.
- Hence, based strictly on the given files, there is **no visible HTTP or other entrypoint** that currently calls `updateAddress`.

So:

- `UserEntity.updateAddress` could affect some routes, but we **do not have evidence** of any route or handler invoking it.
- It is still validated that, whenever some (possibly future or omitted) controller/handler calls `updateAddress`, the new US-zip rule will apply via `new Address(...)`.

I cannot assert any concrete entrypoint for this path without more files.

---

## 6. Flows that reach `UserMapper.toDomain` (and thus `new Address`)

From 3.3, `UserMapper.toDomain` constructs `Address` from database records.

We now find where `UserMapper.toDomain` is used.

### 6.1. In `UserRepository.toDomain` (inheritance, not shown) and its use

We have:

**File:** `src/modules/user/database/user.repository.ts`

```ts
import { UserMapper } from '../user.mapper';
import { UserEntity } from '../domain/user.entity';
import { SqlRepositoryBase } from '@src/libs/db/sql-repository.base';

@Injectable()
export class UserRepository
  extends SqlRepositoryBase<UserEntity, UserModel>
  implements UserRepositoryPort
{
  // ...

  constructor(
    @InjectPool()
    pool: DatabasePool,
    mapper: UserMapper,
    eventEmitter: EventEmitter2,
  ) {
    super(pool, mapper, eventEmitter, new Logger(UserRepository.name)); // <-- pass UserMapper to base
  }

  async findOneByEmail(email: string): Promise<UserEntity> {
    const user = await this.pool.one(
      sql.type(userSchema)`SELECT * FROM "users" WHERE email = ${email}`,
    );

    return this.mapper.toDomain(user);    // <-- explicit call to UserMapper.toDomain
  }
}
```

Also note `UserRepository` extends `SqlRepositoryBase<UserEntity, UserModel>`. That base class may also call `mapper.toDomain`, but its code is not included, so we cannot cite exact lines; we only **know for sure** about the explicit `return this.mapper.toDomain(user)` above.

### 6.2. Where `UserRepository.findOneByEmail` is used

Searching the provided bundle:

- There are **no references** to `findOneByEmail` in the listed files.
- Therefore, we have **no concrete route/entrypoint** we can tie to this method, even though it clearly exists.

Similarly, for generic repository methods like `findOneById`, `findAll`, `findAllPaginated` (from `RepositoryPort`):

**File:** `src/libs/ddd/repository.port.ts`

```ts
export interface RepositoryPort<Entity> {
  insert(entity: Entity | Entity[]): Promise<void>;
  findOneById(id: string): Promise<Option<Entity>>;
  findAll(): Promise<Entity[]>;
  findAllPaginated(params: PaginatedQueryParams): Promise<Paginated<Entity>>;
  delete(entity: Entity): Promise<boolean>;

  transaction<T>(handler: () => Promise<T>): Promise<T>;
}
```

And in `DeleteUserService`, `findOneById` is used, but:

```ts
const found = await this.userRepo.findOneById(command.userId);
```

We do *not* see the actual implementation in `SqlRepositoryBase`, nor how it uses `UserMapper`. It is reasonable to suspect it calls `toDomain`, but without the file we cannot state it as fact.

So the only *explicit* mapper usage we can prove is `UserRepository.findOneByEmail`, which currently has no callers in the provided files.

---

## 7. Other HTTP entrypoints in the user module

We must verify whether any *other* HTTP routes in the bundle could be affected indirectly by `Address`.

### 7.1. Find Users HTTP controller

**File:** `src/modules/user/queries/find-users/find-users.http.controller.ts`

```ts
@Controller(routesV1.version)
export class FindUsersHttpController {
  constructor(private readonly queryBus: QueryBus) {}

  @Get(routesV1.user.root)                               // <-- HTTP GET
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

The handler just maps `UserModel` (database-level type) to DTOs. `UserModel` is defined in `user.repository.ts`:

```ts
export const userSchema = z.object({
  id: z.string().uuid(),
  createdAt: z.preprocess((val: any) => new Date(val), z.date()),
  updatedAt: z.preprocess((val: any) => new Date(val), z.date()),
  email: z.string().email(),
  country: z.string().min(1).max(255),
  postalCode: z.string().min(1).max(20),
  street: z.string().min(1).max(255),
  role: z.nativeEnum(UserRoles),
});

export type UserModel = z.TypeOf<typeof userSchema>;
```

`FindUsersQueryHandler` uses raw SQL and `userSchema` to build `Paginated<UserModel>`:

**File:** `src/modules/user/queries/find-users/find-users.query-handler.ts`

```ts
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
```

Note:

- No `Address` construction here; only plain `string` fields.
- So the `GET` route does not hit `Address.validate` at all.

### 7.2. Delete User HTTP controller

**File:** `src/modules/user/commands/delete-user/delete-user.http-controller.ts`

```ts
@Controller(routesV1.version)
export class DeleteUserHttpController {
  constructor(private readonly commandBus: CommandBus) {}

  @Delete(routesV1.user.delete)                         // <-- HTTP DELETE
  async deleteUser(@Param('id') id: string): Promise<void> {
    const command = new DeleteUserCommand({ userId: id });
    const result: Result<boolean, NotFoundException> =
      await this.commandBus.execute(command);

    match(result, { ... });
  }
}
```

Command and handler:

**File:** `src/modules/user/commands/delete-user/delete-user.service.ts`

```ts
export class DeleteUserCommand {
  readonly userId: string;

  constructor(props: DeleteUserCommand) {
    this.userId = props.userId;
  }
}

@CommandHandler(DeleteUserCommand)
export class DeleteUserService {
  constructor(
    @Inject(USER_REPOSITORY)
    private readonly userRepo: UserRepositoryPort,
  ) {}

  async execute(
    command: DeleteUserCommand,
  ): Promise<Result<boolean, NotFoundException>> {
    const found = await this.userRepo.findOneById(command.userId);
    if (found.isNone()) return Err(new NotFoundException());
    const user = found.unwrap();
    user.delete();                   // <-- only emits UserDeletedDomainEvent
    const result = await this.userRepo.delete(user);
    return Ok(result);
  }
}
```

Here:

- `user.delete()` does not touch `Address` (see `user.entity.ts`: it just adds a `UserDeletedDomainEvent`).
- `userRepo.findOneById` implementation is not shown; it likely uses the mapper but we have no explicit evidence.
- So we **cannot prove** that `Address` is constructed as part of this route, based solely on the given code.

Given lack of the `SqlRepositoryBase` implementation, we stop short of asserting that `DELETE` uses `Address`.

---

## 8. Summary: HTTP routes definitely affected by the change

Based strictly on the provided files and explicit calls:

### 8.1. HTTP route(s) whose processing definitely invokes `Address.validate`

1. **Create User (HTTP POST)**

   - **Route definition:**
     - `@Controller(routesV1.version)` and `@Post(routesV1.user.root)`
     - **File:** `src/modules/user/commands/create-user/create-user.http.controller.ts`
   - **Call chain:**
     1. `CreateUserHttpController.create(@Body() body: CreateUserRequestDto)`
        ```ts
        const command = new CreateUserCommand(body);
        const result = await this.commandBus.execute(command);
        ```
     2. `CommandBus.execute(command)` (NestJS CQRS framework selects handler via decorator).
     3. `CreateUserService` bound to `CreateUserCommand`:
        - **File:** `create-user.service.ts`
        ```ts
        @CommandHandler(CreateUserCommand)
        export class CreateUserService implements ICommandHandler {
          async execute(command: CreateUserCommand, ...) {
            const user = UserEntity.create({
              email: command.email,
              address: new Address({
                country: command.country,
                postalCode: command.postalCode,
                street: command.street,
              }),
            });
            ...
          }
        }
        ```
     4. `new Address({ ... })` → `ValueObject` constructor → `Address.validate(...)` (new ZIP rule):
        - **File:** `value-object.base.ts`
        ```ts
        constructor(props: ValueObjectProps<T>) {
          this.checkIfEmpty(props);
          this.validate(props);           // virtual call
          this.props = props;
        }
        ```
        - **File:** `address.value-object.ts`
        ```ts
        protected validate(props: AddressProps): void {
          ...
          if (props.country === 'US' && !/^\d{5}$/.test(props.postalCode)) {
            throw new ArgumentOutOfRangeException(
              'postalCode is out of range: US postal code must match ^\\d{5}$',
            );
          }
        }
        ```

   Therefore, **the POST create-user HTTP endpoint is directly impacted** by the new US postal code validation.

### 8.2. HTTP routes that are *not* clearly tied to `Address.validate` in the provided code

- **GET users (`FindUsersHttpController.findUsers`)**: Uses raw SQL and `UserModel`, no `Address` construction.
- **DELETE user (`DeleteUserHttpController.deleteUser`)**: Operates on `UserEntity`, but we lack the repository base implementation to show that it constructs an `Address`.

Given the requirement to avoid guessing, I cannot assert that these two routes call `Address.validate`.

---

## 9. Non-HTTP entrypoints also affected

For completeness (even though your task is focused on HTTP entrypoints):

- **CLI**: `CreateUserCliController.createUser` → `CreateUserCommand` → `CommandBus.execute` → `CreateUserService.execute` → `new Address(...)` → `Address.validate`.
- **Message microservice**: `CreateUserMessageController.create` (`@MessagePattern('user.create')`) → same chain.
- **GraphQL mutation**: `CreateUserGraphqlResolver.create` → same chain.

All of those will now enforce the US 5-digit ZIP rule when `country === 'US'`.

---

If you’d like, I can next:

- Map concrete example requests (body JSON and routes) that would now start failing with the new US ZIP rule, and
- Show how exceptions thrown in `Address.validate` surface at the HTTP layer (e.g., as 400 responses, assuming NestJS default exception filter).