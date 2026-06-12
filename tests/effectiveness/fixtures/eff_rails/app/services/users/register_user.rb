module Users
  class RegisterUser
    def initialize(params)
      @params = params
    end

    def call
      user = User.create!(@params)
      Result.success(user: user)
    rescue ActiveRecord::RecordInvalid => e
      Result.failure(e.message)
    end
  end
end
