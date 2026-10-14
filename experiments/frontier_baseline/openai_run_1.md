Below is the full, evidenced call chain from the changed code to every HTTP entrypoint that can exercise it.

The change is inside `Address.validate`:

```ts
// src/modules/user/domain/value-objects/address.value-object.ts

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
  /* US postal codes ... */
  if (props.country === 'US' && !/^\d{5}$/.test(props.postalCode)) {
    throw new ArgumentOutOfRangeException(
      'postalCode is out of range: US postal code must match ^\\d{5}$',
    );
  }
}
```

This is invoked whenever an `Address` value object is constructed. We look for all such constructions and then trace up to HTTP controllers.

---

## 1. Path via `CreateUserHttpController` (HTTP POST /v1/user)

### 1.1 Changed code: `Address.validate`

- File: `src/modules/user/domain/value-objects/address.value-object.ts`
- The new guard is in `protected validate(props: AddressProps): void` as shown above.

`validate` is called from the `ValueObject` base-class constructor:

- File: `src/libs/ddd/value-object.base.ts`
- Constructor:

  ```ts
  export abstract class ValueObject<T> {
    protected readonly props: ValueObjectProps<T>;

    constructor(props: ValueObjectProps<T>) {
      this.checkIfEmpty(props);
      this.validate(props);        // <- calls Address.validate
      this.props = props;
    }

    protected abstract validate(props: ValueObjectProps<T>): void;
  }
  ```

And `Address` extends `ValueObject<AddressProps>`:

- File: `src/modules/user/domain/value-objects/address.value-object.ts`

  ```ts
  export class Address extends ValueObject<AddressProps> { ... }
  ```

So any `new Address(...)` will run `Address.validate(...)` including the new US‑ZIP rule.

### 1.2 `UserEntity.create` constructs an `Address`

- File: `src/modules/user/domain/user.entity.ts`

  ```ts
  import { Address, AddressProps } from './value-objects/address.value-object';
  import {
    CreateUserProps,
    UpdateUserAddressProps,
    UserProps,
    UserRoles,
  } from './user.types';
  ...
  export class UserEntity extends AggregateRoot<UserProps> {
    ...
    static create(create: CreateUserProps): UserEntity {
      const id = randomUUID();
      const props: UserProps = { ...create, role: UserRoles.guest };
      const user = new UserEntity({ id, props });
      user.addEvent(
        new UserCreatedDomainEvent({
          aggregateId: id,
          email: props.email,
          ...props.address.unpack(),
        }),
      );
      return user;
    }
    ...
  }
  ```

`CreateUserProps` includes an `Address` (not created here, but passed in):

- File: `src/modules/user/domain/user.types.ts`

  ```ts
  export interface CreateUserProps {
    email: string;
    address: Address;
  }
  ```

The `Address` instance passed to `UserEntity.create` must already have been constructed, which is where `Address.validate` runs.

### 1.3 `CreateUserService` constructs the `Address`

- File: `src/modules/user/commands/create-user/create-user.service.ts`

  ```ts
  import { Address } from '@modules/user/domain/value-objects/address.value-object';
  ...
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
    ...
  }
  ```

The `new Address({ ... })` here directly triggers `ValueObject`’s constructor → `Address.validate` with the changed logic.

### 1.4 `CreateUserService` is the CQRS command handler for `CreateUserCommand`

- File: `src/modules/user/commands/create-user/create-user.service.ts`

  ```ts
  import { CommandHandler, ICommandHandler } from '@nestjs/cqrs';
  import { CreateUserCommand } from './create-user.command';

  @CommandHandler(CreateUserCommand)
  export class CreateUserService implements ICommandHandler {
    ...
    async execute(command: CreateUserCommand, ...)
  }
  ```

The `@CommandHandler(CreateUserCommand)` decorator binds this `execute` to the `CreateUserCommand` on Nest’s `CommandBus`.

### 1.5 `CommandBus.execute` is invoked from `CreateUserHttpController`

- File: `src/modules/user/commands/create-user/create-user.http.controller.ts`

  ```ts
  @Controller(routesV1.version)
  export class CreateUserHttpController {
    constructor(private readonly commandBus: CommandBus) {}

    ...
    @Post(routesV1.user.root)
    async create(@Body() body: CreateUserRequestDto): Promise<IdResponse> {
      const command = new CreateUserCommand(body);

      const result: Result<AggregateID, UserAlreadyExistsError> =
        await this.commandBus.execute(command);
      ...
    }
  }
  ```

- `CreateUserCommand` just forwards the DTO fields:

  - File: `src/modules/user/commands/create-user/create-user.command.ts`

    ```ts
    export class CreateUserCommand extends Command {
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

- `CreateUserRequestDto` defines the request body fields, including `country` & `postalCode`:

  - File: `src/modules/user/commands/create-user/create-user.request.dto.ts`

    ```ts
    export class CreateUserRequestDto {
      ...
      @ApiProperty({ example: 'France', description: 'Country of residence' })
      @MaxLength(50)
      @MinLength(4)
      @IsString()
      @Matches(/^[a-zA-Z ]*$/)
      readonly country: string;

      @ApiProperty({ example: '28566', description: 'Postal code' })
      @MaxLength(10)
      @MinLength(4)
      @IsAlphanumeric()
      readonly postalCode: string;
      ...
    }
    ```

- The HTTP route itself:

  - `@Controller(routesV1.version)` and `@Post(routesV1.user.root)` establish an HTTP POST at that path (exact string not given in the snippets, but the construct is clear in the controller above).

Nest’s CQRS machinery is not shown in this repo, but we have explicit evidence that:

- `CreateUserHttpController.create` builds `CreateUserCommand` and calls `this.commandBus.execute`.
- `CreateUserService` is decorated with `@CommandHandler(CreateUserCommand)` and has `execute(command: CreateUserCommand)`.

So it is reasonable (and standard NestJS CQRS) to connect those as one hop, but note that the *framework‑internal* dispatch (`CommandBus.execute` → handler.execute) is not shown in the files. The binding via `@CommandHandler` is explicit, so we can assert that *this handler is intended* to be invoked for that command; the internals of how `CommandBus` does it are outside the provided code.

#### 1.6 Summary of path 1

1. HTTP request → `CreateUserHttpController.create`  
   - File: `src/modules/user/commands/create-user/create-user.http.controller.ts`  
   - Decorators `@Controller(routesV1.version)` and `@Post(routesV1.user.root)` define HTTP entrypoint.
2. Controller creates `CreateUserCommand` from request body and calls `commandBus.execute(command)`.  
   - Same file, `create()` method.
3. Nest CQRS dispatches `CreateUserCommand` to `CreateUserService.execute` via `@CommandHandler(CreateUserCommand)`.  
   - File: `src/modules/user/commands/create-user/create-user.service.ts`.
4. `CreateUserService.execute` constructs `new Address({ country, postalCode, street })` and passes it into `UserEntity.create`.  
   - Same file, `execute` method.
5. `new Address(...)` → `ValueObject` constructor → `Address.validate`, which now enforces the US‑ZIP `^\d{5}$` restriction.  
   - `Address` extends `ValueObject`: `src/modules/user/domain/value-objects/address.value-object.ts`.  
   - `ValueObject` constructor calls `this.validate(props)`: `src/libs/ddd/value-object.base.ts`.  
   - Changed code in `Address.validate`: same `address.value-object.ts`.

This HTTP entrypoint is clearly affected: any POST to this route with `country === 'US'` and a non‑5‑digit `postalCode` will now throw `ArgumentOutOfRangeException` from inside `Address.validate`.

---

## 2. Path via `FindUsersHttpController` (HTTP GET /v1/user)

`FindUsers*` paths use `postalCode` in filters and responses, but **do not** construct a new `Address`; they do not reference `Address` or `UserEntity` at all in the handler itself.

- File: `src/modules/user/queries/find-users/find-users.http.controller.ts`

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
      ...
    }
  }
  ```

- Query handler directly hits the DB, with no `Address`:

  - File: `src/modules/user/queries/find-users/find-users.query-handler.ts`

    ```ts
    @QueryHandler(FindUsersQuery)
    export class FindUsersQueryHandler implements IQueryHandler {
      ...
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
        ...
      }
    }
    ```

No `new Address(...)` or call site that could trigger `Address.validate` appears anywhere in this query stack. Therefore:

- This HTTP GET entrypoint **does not invoke** the changed `Address.validate` and is **not directly affected** by the new rule, based on the provided code.

If some code elsewhere hydrated `UserEntity` with `Address` before returning through this controller, that could involve `Address`, but here the controller and handler work with `UserModel` from the DB, not domain objects. The only mapper that constructs `Address` is `UserMapper.toDomain`:

- File: `src/modules/user/user.mapper.ts`

  ```ts
  toDomain(record: UserModel): UserEntity {
    const entity = new UserEntity({
      id: record.id,
      createdAt: new Date(record.createdAt),
      updatedAt: new Date(record.updatedAt),
      props: {
        email: record.email,
        role: record.role,
        address: new Address({
          street: record.street,
          postalCode: record.postalCode,
          country: record.country,
        }),
      },
    });
    return entity;
  }
  ```

But `FindUsersQueryHandler` returns `UserModel` directly to the controller, not `UserEntity`, and `FindUsersHttpController` never calls `UserMapper`. There is no evidence in the provided files that this query path constructs domain entities or addresses, so we must not assert that `Address.validate` is exercised here.

Conclusion: `FindUsersHttpController`’s GET route is **not on a confirmed execution path** to the changed code.

---

## 3. Path via `DeleteUserHttpController` (HTTP DELETE /v1/user/:id)

- File: `src/modules/user/commands/delete-user/delete-user.http-controller.ts`

  ```ts
  @Controller(routesV1.version)
  export class DeleteUserHttpController {
    constructor(private readonly commandBus: CommandBus) {}

    @Delete(routesV1.user.delete)
    async deleteUser(@Param('id') id: string): Promise<void> {
      const command = new DeleteUserCommand({ userId: id });
      const result: Result<boolean, NotFoundException> =
        await this.commandBus.execute(command);
      ...
    }
  }
  ```

Command handler:

- File: `src/modules/user/commands/delete-user/delete-user.service.ts`

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
      const found = await this.userRepo.findOneById(command.userId);
      if (found.isNone()) return Err(new NotFoundException());
      const user = found.unwrap();
      user.delete();
      const result = await this.userRepo.delete(user);
      return Ok(result);
    }
  }
  ```

`UserRepositoryPort.findOneById` returns `UserEntity`:

- File: `src/libs/ddd/repository.port.ts`

  ```ts
  export interface RepositoryPort<Entity> {
    findOneById(id: string): Promise<Option<Entity>>;
    ...
  }
  ```

- and the concrete `UserRepository` extends this for `UserEntity`:

  - File: `src/modules/user/database/user.repository.ts`

    ```ts
    export class UserRepository
      extends SqlRepositoryBase<UserEntity, UserModel>
      implements UserRepositoryPort
    {
      ...
    }
    ```

`UserRepository` very likely uses `UserMapper.toDomain` (which does construct an `Address`) in its base class, but the implementation of `SqlRepositoryBase` is **not** in the provided files. We cannot see a concrete call to `UserMapper.toDomain` or to `new Address(...)` in the delete path code itself.

So:

- There is **no explicit code in the provided delete path** that constructs `Address`.  
- It is plausible that `userRepo.findOneById` internally constructs a `UserEntity` with an `Address` (thus invoking `Address.validate`), via `UserMapper.toDomain`, but that is an inference depending on `SqlRepositoryBase` internals which we do not have.

Therefore, we must state:

- The HTTP DELETE route is a candidate path to the changed code, **but** we cannot prove any hop that actually creates an `Address` from the snippets given. We cannot assert that it definitely exercises `Address.validate`.

---

## 4. Other non‑HTTP entrypoints that definitely hit the changed code (for completeness)

These are not HTTP routes, but confirm other call chains to `Address`:

### 4.1 CLI entrypoint

- File: `src/modules/user/commands/create-user/create-user.cli.controller.ts`

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
      ...
    }
  }
  ```

This path uses the same `CreateUserCommand` and thus the same handler `CreateUserService.execute` and `new Address(...)` as in section 1.

### 4.2 Microservice message entrypoint

- File: `src/modules/user/commands/create-user/create-user.message.controller.ts`

  ```ts
  @Controller()
  export class CreateUserMessageController {
    constructor(private readonly commandBus: CommandBus) {}

    @MessagePattern('user.create')
    async create(message: CreateUserRequestDto): Promise<IdResponse> {
      const command = new CreateUserCommand(message);

      const id = await this.commandBus.execute(command);

      return new IdResponse(id.unwrap());
    }
  }
  ```

Again, same command & handler.

### 4.3 GraphQL entrypoint for create

- File: `src/modules/user/commands/create-user/graphql-example/create-user.graphql-resolver.ts`

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

Same chain to `CreateUserService.execute` and `new Address(...)`.

### 4.4 GraphQL entrypoint for find‑users

- File: `src/modules/user/queries/find-users/find-users.graphql-resolver.ts`

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
      ...
    }
  }
  ```

Like the HTTP GET, this uses `UserModel` directly and does not construct `Address`. So as with section 2, we cannot show that it hits `Address.validate` based on the given code.

---

## 5. `UserMapper` toDomain construction of `Address`

As noted, any call to `UserMapper.toDomain` also hits the new validate logic:

- File: `src/modules/user/user.mapper.ts`

  ```ts
  toDomain(record: UserModel): UserEntity {
    const entity = new UserEntity({
      id: record.id,
      createdAt: new Date(record.createdAt),
      updatedAt: new Date(record.updatedAt),
      props: {
        email: record.email,
        role: record.role,
        address: new Address({
          street: record.street,
          postalCode: record.postalCode,
          country: record.country,
        }),
      },
    });
    return entity;
  }
  ```

But we only see one explicit consumer of `UserMapper`:

- The `UserRepository` constructor receives `mapper: UserMapper` and passes it to its base class:

  - File: `src/modules/user/database/user.repository.ts`

    ```ts
    constructor(
      @InjectPool()
      pool: DatabasePool,
      mapper: UserMapper,
      eventEmitter: EventEmitter2,
    ) {
      super(pool, mapper, eventEmitter, new Logger(UserRepository.name));
    }
    ```

The actual usage of `mapper.toDomain` is presumably inside `SqlRepositoryBase`, which is **not** given; so we cannot tie it to a particular HTTP route with explicit, line‑level evidence.

---

## Final answer: HTTP entrypoints definitely affected

Based strictly on the provided files and explicit call sites:

1. **Create User (HTTP)**: `CreateUserHttpController.create` (`@Post(routesV1.user.root)`)  
   - File: `src/modules/user/commands/create-user/create-user.http.controller.ts`  
   - Path:

     - HTTP POST → `CreateUserHttpController.create`
     - → `new CreateUserCommand(body)` (same file)
     - → `CommandBus.execute(command)` (same file)
     - → `CreateUserService.execute(command)` via `@CommandHandler(CreateUserCommand)`  
       (`src/modules/user/commands/create-user/create-user.service.ts`)
     - → `new Address({ country: command.country, postalCode: command.postalCode, street: command.street })` (same file)
     - → `ValueObject` constructor → `Address.validate` (`src/libs/ddd/value-object.base.ts` plus `src/modules/user/domain/value-objects/address.value-object.ts`)

2. **Other HTTP routes (`FindUsersHttpController`, `DeleteUserHttpController`)**  
   - `FindUsersHttpController` (GET) does not construct `Address` anywhere in the visible path, and its query handler works directly with `UserModel` and raw SQL. There is no explicit hop to `Address` in the provided code.
   - `DeleteUserHttpController` (DELETE) uses `UserRepositoryPort.findOneById`; the repository likely uses `UserMapper.toDomain` → `new Address(...)`, but the actual call is inside `SqlRepositoryBase`, which is not shown. We cannot assert that this HTTP route hits the changed code without that missing implementation.

So, the **only HTTP entrypoint that we can conclusively trace to the modified `Address.validate`** using evidence from the files is:

- `POST <routesV1.version><routesV1.user.root>` handled by `CreateUserHttpController.create`.